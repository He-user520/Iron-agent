"""工具注册中心 — 管理所有工具的定义和调度"""
import logging

from iron.tools.base import BaseTool


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        """注册一个工具"""
        if tool.name in self._tools:
            logging.warning(f"工具 {tool.name} 已注册，将被覆盖")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_all_schemas(self) -> list[dict]:
        """获取所有工具的 schema（给 AI 看的）"""
        return [t.schema for t in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def set_max_output_chars(self, max_chars: int):
        """P4-3: 批量设置所有已注册工具的输出截断阈值"""
        for tool in self._tools.values():
            tool.max_output_chars = max_chars
