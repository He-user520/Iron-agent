"""SubAgentOrchestrator — 子 Agent 编排

让父 Agent 能启动子 Agent 处理子任务，支持并行和超时。
子 Agent 拥有独立 conversation，不污染父 Agent。
"""
import asyncio
import logging
import time
from typing import Optional

from iron.agent.engine import AgentEngine, TaskAgentEngine, VerifyAgent
from iron.tools.base import BaseTool

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

        Args:
            description: 任务描述（供日志/可视化用）
            prompt: 给子 Agent 的完整指令
            agent_type: coder/explore/verify/task
            max_turns: 最大轮次（子 Agent 内部 process 可能不直接支持，
                       此处作为元信息记录，若 process 支持则传入）
            timeout: 超时秒数，超时自动 cancel

        Returns:
            {"success": bool, "output": str, "error": str | None,
             "elapsed": float, "agent_type": str, "description": str}
        """
        start = time.time()

        try:
            engine_class = self._select_agent_class(agent_type)
            sub_engine = engine_class(
                llm=self._parent.llm,
                prompt_builder=self._parent.prompt_builder,
                skills=self._parent.skills,
                config=self._parent.config,
                lsp_client=getattr(self._parent, "_lsp_client", None),
                code_indexer=getattr(self._parent, "_code_indexer", None),
            )

            # 带超时运行
            result_text = await asyncio.wait_for(
                self._run_sub_agent(sub_engine, prompt, max_turns),
                timeout=timeout,
            )

            return {
                "success": True,
                "output": result_text,
                "error": None,
                "elapsed": round(time.time() - start, 3),
                "agent_type": agent_type,
                "description": description,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "output": "",
                "error": f"子 Agent 超时（{timeout}s）",
                "elapsed": round(time.time() - start, 3),
                "agent_type": agent_type,
                "description": description,
            }
        except asyncio.CancelledError:
            # 不吞 CancelledError，向上传播
            raise
        except Exception as e:
            return {
                "success": False,
                "output": "",
                "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - start, 3),
                "agent_type": agent_type,
                "description": description,
            }

    async def run_parallel(self, tasks: list[dict]) -> list[dict]:
        """并行运行多个子 Agent

        Args:
            tasks: [{"description": ..., "prompt": ..., "agent_type": ...}, ...]

        Returns:
            结果列表（顺序与输入一致）。单个任务异常不会影响其他任务。
        """
        coros = [self.run(**t) for t in tasks]
        return await asyncio.gather(*coros, return_exceptions=False)

    def _select_agent_class(self, agent_type: str):
        """根据类型选择 Agent 类"""
        mapping = {
            "coder": AgentEngine,
            "explore": TaskAgentEngine,
            "verify": VerifyAgent,
            "task": TaskAgentEngine,
        }
        return mapping.get(agent_type, TaskAgentEngine)

    async def _run_sub_agent(self, engine, prompt: str, max_turns: int) -> str:
        """运行子 Agent 并收集输出

        engine.process(user_input) 是 async generator，yield (event_type, event_data)。
        收集 chat_chunk 事件文本，遇到 phase=done 时结束。
        max_turns 作为元信息：若 process 支持 max_turns 关键字则传入。
        """
        outputs: list[str] = []
        # process 签名可能不接受 max_turns，用 inspect 兼容
        import inspect
        sig = inspect.signature(engine.process)
        kwargs = {"user_input": prompt}
        if "max_turns" in sig.parameters:
            kwargs["max_turns"] = max_turns

        async for event_type, event_data in engine.process(**kwargs):
            if event_type == "chat_chunk":
                outputs.append(event_data.get("text", ""))
            elif event_type == "phase" and event_data.get("phase") == "done":
                break
            elif event_type == "chat_response":
                # chat_response 事件含完整 message，兜底收集
                msg = event_data.get("message") or event_data.get("content") or ""
                if msg:
                    outputs.append(msg)

        return "".join(outputs).strip()


class TaskTool(BaseTool):
    """task — 启动子 Agent 处理子任务

    让主 Agent 能派发独立子任务（独立 conversation，支持超时和并行）。
    task 工具不加入只读集合（子 Agent 可能触发写操作）。
    """

    def __init__(self, max_output_chars: int = None):
        super().__init__(max_output_chars=max_output_chars)

    @property
    def name(self) -> str:
        return "task"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "task",
                "description": (
                    "启动子 Agent 处理子任务（独立 conversation，支持超时）。"
                    "适用于：代码库探索、方案规划、并行子任务、代码审查。"
                    "子 Agent 不共享父 Agent 的对话历史。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "任务描述（简短一句话，供日志用）",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "给子 Agent 的完整指令（含上下文和目标）",
                        },
                        "agent_type": {
                            "type": "string",
                            "description": "Agent 类型：coder(可写)/explore(只读)/verify(验证)",
                            "default": "explore",
                        },
                        "max_turns": {
                            "type": "integer",
                            "description": "子 Agent 最大轮次（默认 5）",
                            "default": DEFAULT_MAX_TURNS,
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "超时秒数（默认 60）",
                            "default": DEFAULT_TIMEOUT,
                        },
                    },
                    "required": ["description", "prompt"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        """执行子 Agent 任务

        从 context["engine"] 获取父 Agent 引用，创建 SubAgentOrchestrator 运行。
        """
        parent_engine = context.get("engine")
        if parent_engine is None:
            return {
                "success": False,
                "error": "无法获取父 Agent（context 缺少 engine）",
                "output": "",
            }

        # 特性门控
        try:
            from iron.config.features import is_feature_enabled
            if not is_feature_enabled("sub_agents"):
                return {
                    "success": False,
                    "error": "子 Agent 特性未启用（features.sub_agents=False）",
                    "output": "",
                }
        except ImportError:
            pass  # 特性门控不可用时默认允许

        description = args.get("description", "")
        prompt = args.get("prompt", "")
        if not prompt:
            return {"success": False, "error": "缺少 prompt 参数", "output": ""}

        orchestrator = SubAgentOrchestrator(parent_engine)
        result = await orchestrator.run(
            description=description,
            prompt=prompt,
            agent_type=args.get("agent_type", "explore"),
            max_turns=args.get("max_turns", DEFAULT_MAX_TURNS),
            timeout=args.get("timeout", DEFAULT_TIMEOUT),
        )
        return result


def register_task_tool(registry) -> None:
    """注册 task 工具到 registry"""
    registry.register(TaskTool())
