"""Iron 插件系统 — 接口定义

插件接口设计原则：
- 插件不能直接访问 engine 内部状态，必须通过 PluginContext
- 所有文件操作经过 path_guard 校验
- 插件加载失败不影响主进程，记录日志后跳过
- 插件提供的工具继承 BaseTool，自动获得 safe_execute 保护
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from iron.plugins.context import PluginContext

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    """插件清单（plugin.json）

    每个插件必须提供 plugin.json 描述自身元数据。
    """
    name: str                                    # 插件唯一标识（如 "stm32-helper"）
    version: str                                 # 语义化版本（如 "1.0.0"）
    description: str                             # 一句话描述
    author: str = ""                             # 作者
    homepage: str = ""                           # 主页 URL
    min_iron_version: str = "2.8.0"              # 最低兼容 Iron 版本
    permissions: list[str] = field(default_factory=list)  # ["file_read", "file_write", "run_command", "network"]
    entry_point: str = "plugin"                  # 模块入口（iron.plugins.<name>.<entry_point>）


class IronPlugin:
    """插件基类 — 所有插件必须继承

    子类需实现 on_load()，可选实现 on_unload() / get_tools() / get_skills() / get_hooks()。

    用法:
        class MyPlugin(IronPlugin):
            def on_load(self, context):
                self.ctx = context

            def get_tools(self):
                return [MyCustomTool()]
    """

    manifest: PluginManifest

    def on_load(self, context: "PluginContext") -> None:
        """插件加载时调用（仅一次）

        Args:
            context: PluginContext 实例，提供受控的项目访问
        """
        raise NotImplementedError

    def on_unload(self) -> None:
        """插件卸载时调用（仅一次）

        子类可选实现，用于清理资源（关闭文件、子进程等）
        """
        pass

    def get_tools(self) -> list:
        """返回插件提供的工具列表（每个工具继承 BaseTool）

        Returns:
            工具实例列表，空列表表示无工具
        """
        return []

    def get_skills(self) -> list:
        """返回插件提供的 Skill 列表"""
        return []

    def get_hooks(self) -> list:
        """返回插件提供的 Hook 列表（PreToolUse / PostToolUse 等）"""
        return []


# 反模式防护 #8：PluginContext 在 context.py 中定义，此处不导入避免循环
