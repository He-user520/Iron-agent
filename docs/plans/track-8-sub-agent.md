# Track 8: 子 Agent 并行编排（Task 工具）

> **执行者**：Task B  
> **优先级**：P1  
> **依赖**：无  
> **目标**：让 AI 能启动子 Agent 处理复杂任务，支持并行

---

## 1. 背景与价值

- Claude Code：Task 工具可启动任意子 Agent，支持并行
- **Iron v3.0**：VerifyAgent 单层调用，无并行编排

### 本 Track 交付
1 个 `task` 工具 + `SubAgentOrchestrator` 类：

```python
# 工具调用示例
{
    "description": "搜索代码库中所有使用 HAL_Delay 的位置",
    "prompt": "在 src/ 目录下搜索 HAL_Delay 的所有调用点，返回文件名和行号",
    "agent_type": "explore",  # 可选：coder/explore/verify
    "max_turns": 5
}
```

**能力**：
- 子 Agent 独立 conversation，不污染父 Agent
- 超时自动 cancel（默认 60s）
- 结果序列化回父 Agent
- 支持并行启动多个子 Agent（asyncio.gather）

---

## 2. 设计原则

1. **隔离**：子 Agent 不共享父 Agent 的 conversation
2. **超时**：默认 60s，可配置，超时自动 cancel
3. **结果序列化**：子 Agent 的最终输出转为字符串回传
4. **不引入新依赖**：复用现有 AgentEngine
5. **特性门控**：注册 `sub_agents` 特性（默认 True）

---

## 3. 实施步骤

### Step 1: 创建 SubAgentOrchestrator

**文件**：`iron/agent/sub_agent.py`（新建）

```python
"""SubAgentOrchestrator — 子 Agent 编排

让父 Agent 能启动子 Agent 处理子任务，支持并行和超时。
"""
import asyncio
import logging
from typing import Optional

from iron.agent.engine import AgentEngine, TaskAgentEngine, VerifyAgent

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60  # 秒
DEFAULT_MAX_TURNS = 5


class SubAgentOrchestrator:
    """子 Agent 编排器

    用法:
        orchestrator = SubAgentOrchestrator(parent_engine)
        result = await orchestrator.run(
            description="搜索 HAL_Delay",
            prompt="在 src/ 下搜索 HAL_Delay 调用点",
            agent_type="explore",
        )
    """

    def __init__(self, parent_engine: AgentEngine):
        self._parent = parent_engine

    async def run(
        self,
        description: str,
        prompt: str,
        agent_type: str = "explore",
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        """运行单个子 Agent

        Returns:
            {"success": bool, "output": str, "error": str | None,
             "elapsed": float, "agent_type": str}
        """
        import time
        start = time.time()

        try:
            # 创建子 Agent（独立 conversation）
            engine_class = self._select_agent_class(agent_type)
            sub_engine = engine_class(
                llm=self._parent.llm,
                prompt_builder=self._parent.prompt_builder,
                skills=self._parent.skills,
                config=self._parent.config,
                lsp_client=getattr(self._parent, "_lsp_client", None),
                code_indexer=getattr(self._parent, "_code_indexer", None),
            )

            # 运行（带超时）
            result_text = await asyncio.wait_for(
                self._run_sub_agent(sub_engine, prompt, max_turns),
                timeout=timeout,
            )

            return {
                "success": True,
                "output": result_text,
                "error": None,
                "elapsed": time.time() - start,
                "agent_type": agent_type,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "output": "",
                "error": f"子 Agent 超时（{timeout}s）",
                "elapsed": time.time() - start,
                "agent_type": agent_type,
            }
        except Exception as e:
            return {
                "success": False,
                "output": "",
                "error": f"{type(e).__name__}: {e}",
                "elapsed": time.time() - start,
                "agent_type": agent_type,
            }

    async def run_parallel(self, tasks: list[dict]) -> list[dict]:
        """并行运行多个子 Agent

        Args:
            tasks: [{"description": ..., "prompt": ..., "agent_type": ...}, ...]

        Returns:
            结果列表（顺序与输入一致）
        """
        coros = [self.run(**t) for t in tasks]
        return await asyncio.gather(*coros, return_exceptions=True)

    def _select_agent_class(self, agent_type: str):
        """根据类型选择 Agent 类"""
        mapping = {
            "coder": AgentEngine,
            "explore": TaskAgentEngine,
            "verify": VerifyAgent,
            "task": TaskAgentEngine,
        }
        return mapping.get(agent_type, TaskAgentEngine)

    async def _run_sub_agent(self, engine, prompt: str,
                              max_turns: int) -> str:
        """运行子 Agent 并收集输出"""
        outputs = []
        async for event_type, event_data in engine.process(
            user_input=prompt,
            max_turns=max_turns,
        ):
            if event_type == "chat_chunk":
                outputs.append(event_data.get("text", ""))
            elif event_type == "phase" and event_data.get("phase") == "complete":
                break
        return "".join(outputs).strip()
```

---

### Step 2: 创建 TaskTool 工具

**文件**：`iron/agent/sub_agent.py`（同文件追加）

```python
from iron.tools.base import BaseTool


class TaskTool(BaseTool):
    """task — 启动子 Agent 处理子任务"""

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return "启动子 Agent 处理子任务（独立 conversation，支持超时）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "任务描述"},
                "prompt": {"type": "string", "description": "给子 Agent 的完整指令"},
                "agent_type": {"type": "string",
                              "description": "Agent 类型：coder/explore/verify",
                              "default": "explore"},
                "max_turns": {"type": "integer", "default": 5},
                "timeout": {"type": "integer", "default": 60},
            },
            "required": ["description", "prompt"],
        }

    async def execute_async(self, args: dict, context: dict) -> dict:
        """异步执行（子 Agent 需要异步）"""
        parent_engine = context.get("engine")
        if parent_engine is None:
            return {"success": False, "error": "无法获取父 Agent",
                    "output": ""}

        try:
            from iron.config.features import is_feature_enabled
            if not is_feature_enabled("sub_agents"):
                return {"success": False,
                        "error": "子 Agent 特性未启用（features.sub_agents=False）",
                        "output": ""}
        except ImportError:
            pass  # 特性门控不可用时默认允许

        orchestrator = SubAgentOrchestrator(parent_engine)
        result = await orchestrator.run(
            description=args["description"],
            prompt=args["prompt"],
            agent_type=args.get("agent_type", "explore"),
            max_turns=args.get("max_turns", 5),
            timeout=args.get("timeout", 60),
        )
        return result

    def execute(self, args: dict, context: dict) -> dict:
        """同步包装（实际调用 execute_async）"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在事件循环中，创建 task 但无法等待
                # 这种情况下应由 engine 直接调用 execute_async
                future = asyncio.ensure_future(
                    self.execute_async(args, context))
                return {"success": False,
                        "error": "已在事件循环中，请调用 execute_async",
                        "output": ""}
            else:
                return loop.run_until_complete(
                    self.execute_async(args, context))
        except RuntimeError:
            return asyncio.run(self.execute_async(args, context))


def register_task_tool(registry) -> None:
    registry.register(TaskTool())
```

---

### Step 3: 注册到 engine.py

**文件**：`iron/agent/engine.py`

```python
# v4.0: 子 Agent 编排工具
try:
    from iron.agent.sub_agent import register_task_tool
    register_task_tool(self._tool_registry)
except ImportError:
    logger.warning("sub_agent 模块加载失败")
```

**关键**：在工具执行的 context 中注入 `engine=self`，让 TaskTool 能拿到父 engine：
```python
context = {
    # ... 现有字段 ...
    "engine": self,  # v4.0: TaskTool 需要父 engine 引用
}
```

`task` 工具不加入只读集合（会触发子 Agent 的写操作）。

---

### Step 4: 注册特性门控

**文件**：`iron/config/features.py`

```python
"sub_agents": True,  # v4.0: 子 Agent 并行编排
```

---

### Step 5: 创建测试

**文件**：`tests/test_sub_agent.py`（新建）

至少 12 个测试：
- SubAgentOrchestrator.run 成功
- 超时返回 False
- agent_type 选择正确
- run_parallel 并行执行
- TaskTool.execute_async 成功
- TaskTool 同步包装
- 特性门控关闭时返回错误
- 父 engine 缺失时返回错误
- 子 Agent conversation 隔离
- 子 Agent 失败时错误回传
- max_turns 限制
- 注册到 registry

---

### Step 6: 全量验证

```bash
python -m pytest tests/test_sub_agent.py -v
python -m pytest tests/test_engine.py tests/test_engine_integration.py -v
```

---

## 4. 完成标准

- [ ] SubAgentOrchestrator 实现
- [ ] TaskTool 工具实现
- [ ] engine.py 注册工具 + context 注入 engine
- [ ] features.py 注册特性
- [ ] 12+ 测试通过
- [ ] 回归测试 0 失败

---

## 5. 风险点

1. **死锁**：父 Agent 在事件循环中调用同步 execute，需用 execute_async
2. **递归深度**：子 Agent 启动子子 Agent 可能无限递归，需限制深度（默认 max_depth=2）
3. **资源泄漏**：子 Agent 的 MCP/LSP 客户端必须显式清理
4. **conversation 隔离**：子 Agent 不能修改父 Agent 的 conversation
5. **engine.py context 注入 self**：可能引入循环引用，但 Python GC 可处理

---

## 6. 不在本 Track 范围

- 子 Agent 的子 Agent（递归编排，留给 V4.1）
- 子 Agent 工具结果可视化（如子 Agent 的 diff 预览）
- 子 Agent 权限继承策略（当前：子 Agent 独立权限回调）
