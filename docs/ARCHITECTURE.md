# Iron 架构说明

> L1-L7 七层架构设计，模块职责与协作流程

## 目录

1. [L1-L7 七层架构](#1-l1-l7-七层架构)
2. [模块职责表](#2-模块职责表)
3. [Agent 类型与协作流程](#3-agent-类型与协作流程)
4. [工具调用流程](#4-工具调用流程)
5. [LLM 流式恢复机制（三态）](#5-llm-流式恢复机制三态)
6. [特性门控设计](#6-特性门控设计)
7. [反模式防护](#7-反模式防护)

---

## 1. L1-L7 七层架构

Iron 采用分层架构，每层职责单一、依赖单向（上层依赖下层，下层不感知上层）。

```
+--------------------------------------------------+
|  L7 长期扩展层  | code_indexer / lsp / plugins  |  tree-sitter / LSP / 插件系统
+--------------------------------------------------+
|  L6 UI 层       | cli/main / ui / vim / theme   |  交互式 REPL + 渲染
+--------------------------------------------------+
|  L5 服务层      | core/db / pubsub / memory     |  持久化 + 事件总线 + 记忆
+--------------------------------------------------+
|  L4 权限层      | rules / permission / hooks    |  DSL 规则 + Pre/Post Hooks
+--------------------------------------------------+
|  L3 工具层      | tools/* / llm/backend         |  28+ 工具 + LLM 后端
+--------------------------------------------------+
|  L2 内核层      | agent/engine / context        |  Agentic Loop + 上下文管理
+--------------------------------------------------+
|  L1 入口层      | __main__ / config / bootstrap |  CLI 入口 + 配置加载
+--------------------------------------------------+
```

### 依赖方向

- L1 → L2 → L3 → L4 → L5 → L6 → L7（上层依赖下层）
- L7 不被其他层依赖（可选扩展）
- 同层模块可横向依赖（如 L3 的 tools 可调用 L3 的 llm）

### 反向依赖禁止

- L2 内核不直接调用 L6 UI（通过 AgentEvent yield 解耦）
- L3 工具不直接调用 L4 权限（权限由 L2 engine 在调度前检查）
- L5 服务不感知 L2 内核状态（通过事件总线订阅）

---

## 2. 模块职责表

14 个顶层包的职责与关键文件：

| 包 | 层 | 职责 | 关键文件 |
|----|---|------|---------|
| `iron/` | L1 | 入口 + 版本 + 常量 | `__main__.py`, `__init__.py`, `constants.py` |
| `iron/config/` | L1 | 配置 + 特性门控 | `settings.py`, `features.py` |
| `iron/agent/` | L2 | Agentic Loop + 上下文 + 记忆 | `engine.py`, `context_compactor.py`, `memory.py` |
| `iron/agent/agents/` | L2 | Agent 配置 | `build.md`, `embed.md`, `explore.md`, `plan.md`, `verify.md` |
| `iron/llm/` | L3 | LLM 后端 + Prompt 缓存 | `backend.py`, `prompt_cache.py` |
| `iron/tools/` | L3 | 28+ 工具 + 注册表 | `registry.py`, `edit_file.py`, `git_tools.py`, `multi_edit.py` |
| `iron/rules/` | L4 | 规则引擎 | `iron_rules.py`, `ai_antipatterns.py`, `permission_rules.py` |
| `iron/agent/permission.py` | L4 | 权限管理 | — |
| `iron/agent/hooks.py` | L4 | Pre/Post Hooks | — |
| `iron/core/` | L5 | SQLite + 事件总线 | `db.py`, `pubsub.py`, `migrations/` |
| `iron/agent/memory.py` | L5 | 长期记忆 | — |
| `iron/cli/` | L6 | CLI 交互 | `main.py`, `ui.py`, `vim.py`, `theme.py` |
| `iron/cli/commands/` | L6 | 斜杠命令分组 | `file_cmds.py`, `git_cmds.py`, `metrics_cmds.py` |
| `iron/integrations/` | L7 | 集成扩展 | `code_indexer.py`, `lsp_client.py`, `embedforge_bridge.py` |
| `iron/plugins/` | L7 | 插件系统 | `base.py`, `manager.py`, `context.py` |
| `iron/remote/` | L7 | 远程 SSH | `ssh_client.py`, `executor.py` |
| `iron/security/` | L7 | OS 沙箱 | `sandbox.py` |
| `iron/skills/` | L7 | 技能系统 | `registry.py`, `base.py`, `executable.py` |
| `iron/mcp/` | L7 | MCP 客户端 | `client.py` |
| `iron/utils/` | — | 工具函数 | `metrics.py`, `token_counter.py`, `doc_reader.py` |

---

## 3. Agent 类型与协作流程

### 5 个 Agent 类型

| Agent | 配置文件 | 工具白名单 | 适用场景 |
|-------|---------|-----------|---------|
| Coder | `agents/build.md` | 全部工具 | 默认编码 Agent，可写文件 / 编译 / 烧录 |
| Task | — | 全部工具 | 通用任务 Agent（可派生子 Agent 并行执行） |
| Verify | `agents/verify.md` | 只读工具 | 验证代码质量（静态分析 + LSP + 编译） |
| Explore | `agents/explore.md` | 只读工具 | 探索代码库（search / read / find_files） |
| Base | — | — | 无工具的基础 Agent（用于子 Agent 编排） |

### 切换 Agent

```
> /agent
1. Coder (当前)
2. Task
3. Verify
4. Explore
选择: 3
已切换到 Verify Agent
```

### 协作流程

#### Coder 主流程

```
用户输入 /code <需求>
    |
    v
[Coder Agent]
    |
    +-- 1. 思考（LLM 流式）
    +-- 2. 调用工具（write_file / edit_file / run_command）
    +-- 3. 把工具结果送回 LLM
    +-- 4. 循环 1-3，直到 LLM 调用 chat() 终止
    |
    v
用户看到最终回复
```

#### Verify 验证流程

```
> /verify
    |
    v
[Verify Agent] (只读)
    |
    +-- 1. 读取项目文件（read_file / find_files）
    +-- 2. 静态分析（embed_lint / lsp_diagnostics）
    +-- 3. 编译检查（embed_build）
    +-- 4. 用 chat() 报告问题
    |
    v
用户看到验证报告
```

#### Task 子 Agent 编排（v4.0）

```
> /code 实现串口通信 + 添加 LED 闪烁 + 写单元测试
    |
    v
[Coder Agent]
    |
    +-- 1. 思考：拆分为 3 个子任务
    +-- 2. 调用 task 工具派生子 Agent
    |       |
    |       +-- [子 Agent 1: 串口通信]  (并行)
    |       +-- [子 Agent 2: LED 闪烁]  (并行)
    |       +-- [子 Agent 3: 单元测试]  (并行)
    |
    +-- 3. 子 Agent 结果序列化回父 Agent
    +-- 4. 父 Agent 汇总，用 chat() 报告
    |
    v
用户看到汇总结果
```

子 Agent 不共享父 Agent 的 conversation（避免污染），超时自动 cancel。

---

## 4. 工具调用流程

### 完整流程（engine.py process 方法）

```
用户输入
    |
    v
process(user_input)
    |
    +-- _init_session(user_input)
    |       |
    |       +-- 记忆整理（dream_distill，可选）
    |       +-- 状态重置（_recent_calls / _files_created / _files_modified）
    |       +-- Skill 匹配
    |       +-- 系统提示构建
    |       +-- Prompt Cache 命中检查
    |       +-- MCP 连接（首次）
    |
    +-- for step in range(MAX_STEPS):
    |       |
    |       +-- compact_pipeline(conversation, system)
    |       |       |
    |       |       +-- Level 1: microcompact（截断早期工具输出）
    |       |       +-- Level 2: compact_if_needed（动态阈值）
    |       |       +-- Level 3-5: 高级压缩（按需）
    |       |
    |       +-- _handle_thinking_phase(system, messages, step, tools)
    |       |       |
    |       |       +-- yield thinking 事件
    |       |       +-- yield phase=THINK 事件
    |       |       +-- LLM 流式调用（stream_generate）
    |       |       +-- 三态处理（complete / partial / failed）
    |       |       +-- 失败时 fallback 到非流式 generate
    |       |       +-- 返回 LLMResponse（通过 _thinking_resp 实例属性）
    |       |
    |       +-- _parse_tool_calls(resp)
    |       |       |
    |       |       +-- 标准格式：resp.tool_calls
    |       |       +-- 兼容格式：从 content 解析 markdown JSON 代码块
    |       |       +-- HC-4：partial 响应跳过文本兼容解析（is_partial=True）
    |       |
    |       +-- if not tool_calls:  # AI 给出最终回复
    |       |       yield chat_response
    |       |       break
    |       |
    |       +-- yield phase=EXECUTE
    |       +-- _filter_tool_calls_by_permission(tool_calls)
    |       |       |
    |       |       +-- 只读 Agent：白名单过滤
    |       |       +-- 被阻止工具：yield tool_blocked + 加入 tool_results
    |       |
    |       +-- for call in tool_calls:
    |       |       |
    |       |       +-- _check_pre_tool_gates(name, args)
    |       |       |       |
    |       |       |       +-- 黑名单检查
    |       |       |       +-- DSL 规则引擎
    |       |       |       +-- PreToolUse hooks（插件）
    |       |       |       +-- 返回：放行 / 阻止 / 修改 args
    |       |       |
    |       |       +-- _dispatch_tool_call(call)
    |       |       |       |
    |       |       |       +-- chat → _handle_chat_tool (终止)
    |       |       |       +-- write_file → _handle_write_file_tool
    |       |       |       +-- edit_file → _handle_edit_file_tool
    |       |       |       +-- run_command → _handle_run_command_tool
    |       |       |       +-- read_file → _handle_read_file_tool
    |       |       |       +-- else → _handle_external_tool
    |       |       |       |       |
    |       |       |       |       +-- 工具注册表查找（tool_registry）
    |       |       |       |       +-- 只读工具并行执行（_pending_readonly）
    |       |       |       |       +-- 破坏性工具授权回调
    |       |       |       |       +-- BaseTool.execute(args, ctx)
    |       |       |       |       +-- metrics.record("tool_calls", ...)
    |       |       |       |
    |       |       |       +-- yield tool_start / tool_result / tool_error
    |       |
    |       +-- _handle_post_step(step, resp, tool_results, tool_calls)
    |               |
    |               +-- 任务完成检测（task_track）
    |               +-- 步数预警（剩余 5/1 步）
    |               +-- 对话历史 append（assistant + tool 结果）
    |               +-- 失败工具检测
    |               +-- Stop Hooks 收敛检测（任一触发 → break）
    |
    +-- 循环结束
```

### 工具注册表

工具注册在 `iron/tools/registry.py`，按 Agent 类型过滤：

```python
# registry.py
class ToolRegistry:
    def get_tools_for_agent(self, agent_type: str) -> list[dict]:
        """返回 Agent 可用的工具列表（OpenAI tools 格式）"""
        allowed = self._get_allowed_tools(agent_type)
        return [t.to_openai_schema() for t in self._tools if t.name in allowed]
```

工具继承 `BaseTool`：

```python
# tools/base.py
class BaseTool(ABC):
    name: str
    description: str
    parameters: dict  # JSON Schema

    @abstractmethod
    async def execute(self, args: dict, ctx: dict) -> dict:
        """执行工具，返回 {"success": bool, "output": str, ...}"""
```

---

## 5. LLM 流式恢复机制（三态）

### 设计目标

- 流式中断时保留已接收 chunk（避免数据丢失，HC-2）
- 已接收 chunk 时不重发请求（避免双倍 token 消耗，HC-1）
- 完全失败（0 chunk）时允许 fallback 到非流式（HC-3）
- partial 响应的不完整 JSON 不传给工具调用解析（HC-4）

### 数据结构

```python
# iron/llm/backend.py
@dataclass
class StreamBuffer:
    chunks: list = field(default_factory=list)
    accumulated_text: str = ""
    is_complete: bool = False
    failure_reason: str | None = None
    chunks_received: int = 0

    def append(self, chunk: str) -> None: ...
    def flush(self) -> str: ...
    def is_partial(self) -> bool: ...
    def is_empty(self) -> bool: ...
    def mark_complete(self) -> None: ...
    def mark_failed(self, reason: str) -> None: ...


@dataclass
class StreamResult:
    state: str  # "complete" | "partial" | "failed"
    content: str = ""
    tool_calls: list | None = None
    model: str = ""
    usage: dict = field(default_factory=dict)
    error: str | None = None
    chunks_received: int = 0
```

### 协议

```python
# stream_generate 事件协议
async for event_type, event_data in backend.stream_generate(...):
    if event_type == "chunk":
        # 文本增量（str）
        ui.render_chunk(event_data)
    elif event_type == "result":
        # 终止事件（StreamResult 三态）
        sr = event_data
        if sr.is_complete:
            # 正常完成
        elif sr.is_partial:
            # 中断但已收内容（不重发）
        else:  # failed
            # 0 chunk 失败（允许 fallback）
```

### 三态决策表

| StreamResult.state | chunks_received | 行为 | 重发 | AgentEvent |
|---|---|---|---|---|
| complete | ≥0 | 用 content + tool_calls 构造 LLMResponse | 否 | 无 |
| partial | >0 | 用 content 构造 LLMResponse（tool_calls=None） | **否（HC-1）** | stream_partial |
| failed | 0 | fallback 到 generate() | **是（HC-3）** | thinking |

### HC-4 保护

partial 响应的 `LLMResponse.is_partial=True`，`_parse_tool_calls` 检测到该标记时跳过文本兼容解析：

```python
def _parse_tool_calls(self, resp: LLMResponse) -> list[dict]:
    if resp.tool_calls:
        return self._parse_standard_tool_calls(resp.tool_calls)
    if getattr(resp, "is_partial", False):
        return []  # HC-4: partial 跳过文本兼容解析
    return self._parse_text_compat_tool_calls(resp.content)
```

---

## 6. 特性门控设计

### 设计原则

- **集中管理**：所有特性在 `iron/config/features.py` 的 `DEFAULT_FEATURES` 字典中声明
- **默认值合理**：已实现功能默认 True，可选/实验性默认 False
- **用户覆盖**：`~/.iron/features.yml` 文件覆盖默认值
- **全局单例**：`get_feature_flags()` 提供进程级单例
- **安全降级**：加载失败时回退到默认值，不阻塞主流程

### 用法

```python
from iron.config.features import is_feature_enabled

if is_feature_enabled("metrics"):
    from iron.utils.metrics import counter
    counter("tool_calls", tags={"tool": name})
```

### 特性列表

详见 [用户指南 § 5](USER_GUIDE.md#5-特性门控配置)。

### 添加新特性

1. 在 `DEFAULT_FEATURES` 字典中添加键值对
2. 在代码中用 `is_feature_enabled("xxx")` 检查
3. 更新 `docs/USER_GUIDE.md` 特性列表
4. 添加测试（`tests/test_features.py`）

---

## 7. 反模式防护

Iron 内置 8 项反模式检测（`iron/rules/ai_antipatterns.py`），在 PreToolUse 钩子中拦截：

| # | 反模式 | 检测方式 | 处置 |
|---|--------|---------|------|
| 1 | 路径越界 | `path_guard` 校验 `..` 跨目录 | 阻止 + 提示 |
| 2 | 命令注入 | `;` / `&` / `\|` / `\n` / `\r` 元字符检测 | 阻止 + 提示 |
| 3 | 敏感信息泄漏 | API key / password / token 正则匹配输出 | 脱敏 + 警告 |
| 4 | SSRF | URL 白名单 + 内网地址检测 | 阻止 + 提示 |
| 5 | 无限循环 | doom_loop 检测器（连续相同工具调用） | 阻止 + 提示 |
| 6 | 工具滥用 | `_recent_calls` 滑动窗口检测重复调用 | 警告 |
| 7 | 上下文爆炸 | ContextCompactor 阈值检测 | 触发压缩 |
| 8 | 静默失败 | `success=False` 工具结果检测 | yield step_done + 失败计数 |

### 反模式规则示例

```python
# iron/rules/ai_antipatterns.py
class AiAntipatternRules:
    def check_pre_tool_use(self, tool_name: str, args: dict) -> dict:
        if tool_name == "run_command":
            cmd = args.get("command", "")
            if self._has_injection(cmd):
                return {"_block": True, "reason": "命令注入风险"}
        if tool_name in ("write_file", "edit_file"):
            path = args.get("path", "")
            if self._is_path_traversal(path):
                return {"_block": True, "reason": "路径越界"}
        return {"_block": False}
```

---

## 附录：关键文件速查

| 文件 | 行数 | 职责 |
|------|------|------|
| [iron/agent/engine.py](file:///iron/agent/engine.py) | ~1700 | Agentic Loop 主流程 |
| [iron/agent/context_compactor.py](file:///iron/agent/context_compactor.py) | — | 5 层上下文压缩管道 |
| [iron/agent/memory.py](file:///iron/agent/memory.py) | — | 4 层记忆系统 + checkpoint |
| [iron/llm/backend.py](file:///iron/llm/backend.py) | ~970 | 4 个 LLM 后端 + StreamBuffer |
| [iron/tools/registry.py](file:///iron/tools/registry.py) | — | 工具注册表 |
| [iron/tools/base.py](file:///iron/tools/base.py) | — | BaseTool 抽象基类 |
| [iron/config/features.py](file:///iron/config/features.py) | ~230 | 特性门控 |
| [iron/cli/main.py](file:///iron/cli/main.py) | ~1300 | CLI 主循环 + 命令分发 |
| [iron/cli/ui.py](file:///iron/cli/ui.py) | ~1300 | prompt_toolkit 交互层 |
| [iron/utils/metrics.py](file:///iron/utils/metrics.py) | ~150 | 观测性指标采集 |

---

## 版本

本文档基于 Iron v3.0.0 / v4.0 开发中版本。
