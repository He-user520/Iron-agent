"""记忆工具 — 让 AI 主动保存项目知识到 MEMORY.md（跨会话持久化）"""
from iron.tools.base import BaseTool


class RememberTool(BaseTool):
    """保存记忆到项目持久记忆文件

    当用户说"记住：..."、"以后都用..."、"这个项目的约定是..."时调用此工具。
    保存的内容会在下次会话启动时自动注入到系统提示。
    """

    @property
    def name(self) -> str:
        return "remember"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "remember",
                "description": (
                    "保存知识到项目持久记忆（跨会话保留）。"
                    "当用户要求记住某事、或你发现了值得长期保留的项目约定时调用。"
                    "保存的内容会在下次会话自动注入。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": (
                                "记忆章节名，如：项目约定 / 技术栈 / 已知问题 / "
                                "硬件配置 / 编码规范 / 用户偏好"
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "要记住的内容（一条简短知识）",
                        },
                    },
                    "required": ["section", "content"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        section = args.get("section", "").strip()
        content = args.get("content", "").strip()

        if not section or not content:
            return {
                "success": False,
                "error": "remember 需要 section 和 content 参数",
            }

        # 从 context 获取 engine 的 ProjectMemory 实例
        engine = context.get("engine")
        if engine is None or not hasattr(engine, "_memory"):
            return {
                "success": False,
                "error": "记忆系统未初始化",
            }

        try:
            engine._memory.append_to_memory(section, content)
            return {
                "success": True,
                "section": section,
                "content": content,
                "message": f"已记住（章节: {section}）",
            }
        except (OSError, RuntimeError, ValueError) as e:
            return {"success": False, "error": f"保存记忆失败: {e}"}
