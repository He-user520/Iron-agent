"""ask_user 工具 — 向用户提问（参考 OpenCode question 工具）"""
import asyncio
import inspect

from iron.tools.base import BaseTool


class AskUserTool(BaseTool):
    """向用户提问 — AI 可以在执行过程中向用户确认信息或征求意见"""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "向用户提问。用于确认需求、征求意见、或在多个方案中让用户选择。不要滥用，只在确实需要用户输入时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "要问用户的问题"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选的选项列表（用户也可以自由输入）",
                        },
                    },
                    "required": ["question"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        question = args.get("question", "")
        options = args.get("options", [])

        if not question:
            return {"success": False, "error": "缺少 question 参数"}

        # options 类型校验：必须是字符串列表
        if not isinstance(options, list):
            return {"success": False, "error": "options 必须是字符串列表"}

        # 通过 callback 向用户提问
        # callback 可能是同步函数（内部用阻塞的 pt_prompt）或 async 函数
        # 同步 callback 必须用 asyncio.to_thread 包装，否则会卡住事件循环，
        # 导致 prompt_toolkit 的 Application.run_async 协程无法被正确 await
        callback = context.get("question_callback")
        if callback:
            try:
                if inspect.iscoroutinefunction(callback):
                    answer = await callback(question, options)
                else:
                    answer = await asyncio.to_thread(callback, question, options)
                return {"success": True, "question": question, "answer": answer}
            except (EOFError, RuntimeError, ValueError, OSError) as e:
                return {"success": False, "error": f"提问失败: {e}"}

        # 没有 callback（无 UI 场景），明确告知 AI 未获得真实用户输入
        return {
            "success": True,
            "question": question,
            "answer": None,
            "need_user_input": True,
        }

