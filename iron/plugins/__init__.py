"""Iron 插件系统 — 包初始化

导出公共 API：
- IronPlugin: 插件基类
- PluginManifest: 插件清单
- PluginContext: 插件运行时上下文
- PluginManager: 插件管理器
"""
from iron.plugins.base import IronPlugin, PluginManifest
from iron.plugins.context import PluginContext
from iron.plugins.manager import PluginManager

__all__ = [
    "IronPlugin",
    "PluginManifest",
    "PluginContext",
    "PluginManager",
]
