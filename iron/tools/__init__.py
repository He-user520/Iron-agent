"""工具系统 — 模块化工具注册与执行"""
from iron.tools.base import BaseTool
from iron.tools.registry import ToolRegistry
from iron.tools.edit_file import EditFileTool
from iron.tools.patch_tool import PatchTool
from iron.tools.search_code import SearchCodeTool
from iron.tools.find_files import FindFilesTool
from iron.tools.ask_user import AskUserTool
from iron.tools.task_track import TaskTrackTool
from iron.tools.embed_build import EmbedBuildTool
from iron.tools.embed_flash import EmbedFlashTool
from iron.tools.embed_lint import EmbedLintTool
from iron.tools.remember import RememberTool
from iron.tools.web_search import WebSearchTool
from iron.tools.skill_create import SkillCreateTool
from iron.tools.mcp_config import McpConfigTool


def create_default_registry() -> ToolRegistry:
    """创建默认工具注册中心，注册所有内置工具"""
    registry = ToolRegistry()
    registry.register(EditFileTool())
    registry.register(PatchTool())
    registry.register(SearchCodeTool())
    registry.register(FindFilesTool())
    registry.register(AskUserTool())
    registry.register(TaskTrackTool())
    registry.register(EmbedBuildTool())
    registry.register(EmbedFlashTool())
    registry.register(EmbedLintTool())
    registry.register(RememberTool())
    registry.register(WebSearchTool())
    registry.register(SkillCreateTool())
    registry.register(McpConfigTool())
    return registry
