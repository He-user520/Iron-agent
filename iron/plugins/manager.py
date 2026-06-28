"""PluginManager — 插件管理器

负责插件的发现、加载、卸载、查询。

加载流程：
1. discover() 扫描插件目录，返回所有可用插件清单
2. load(name) 通过 importlib 加载插件模块，实例化插件类，调用 on_load(context)
3. get_all_tools() 聚合所有已加载插件的工具

错误处理：
- 插件加载失败（import 失败、on_load 抛异常）记录日志，不影响其他插件
- 插件清单缺失或格式错误跳过该插件
- 已加载的插件重复 load 返回 False
"""
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from iron.plugins.base import IronPlugin, PluginManifest
from iron.plugins.context import PluginContext

logger = logging.getLogger(__name__)


class PluginManager:
    """插件管理器 — 加载/卸载/查询

    用法:
        ctx = PluginContext(project_root="/path", permissions=["file_read"])
        mgr = PluginManager("/path/.iron-agent/plugins", ctx)
        manifests = mgr.discover()
        mgr.load("my-plugin")
        tools = mgr.get_all_tools()
    """

    def __init__(self, plugins_dir: str, context: PluginContext):
        self._plugins_dir = Path(plugins_dir)
        self._context = context
        self._loaded: dict[str, IronPlugin] = {}
        self._manifests: dict[str, PluginManifest] = {}

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    def discover(self) -> list[PluginManifest]:
        """扫描插件目录，返回所有可用插件清单

        每个插件目录应包含 plugin.json 文件。
        格式错误或缺失的插件跳过并记录警告。
        """
        manifests = []
        if not self._plugins_dir.exists():
            return manifests

        for plugin_dir in self._plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            # 跳过 __pycache__ 等隐藏目录
            if plugin_dir.name.startswith("_") or plugin_dir.name.startswith("."):
                continue
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = PluginManifest(
                    name=data["name"],
                    version=data["version"],
                    description=data.get("description", ""),
                    author=data.get("author", ""),
                    homepage=data.get("homepage", ""),
                    min_iron_version=data.get("min_iron_version", "2.8.0"),
                    permissions=data.get("permissions", []),
                    entry_point=data.get("entry_point", "plugin"),
                )
                manifests.append(manifest)
                self._manifests[manifest.name] = manifest
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("插件清单解析失败 %s: %s", plugin_dir.name, e)
                continue
        return manifests

    def load(self, name: str) -> bool:
        """加载指定插件

        Args:
            name: 插件名（对应 plugin.json 的 name 字段）

        Returns:
            True 表示加载成功；False 表示已加载、清单缺失、或加载失败
        """
        # 已加载直接返回 False
        if name in self._loaded:
            logger.warning("插件 %s 已加载，不能重复加载", name)
            return False

        # 确保 manifest 已 discover
        if name not in self._manifests:
            self.discover()
        manifest = self._manifests.get(name)
        if manifest is None:
            logger.warning("插件 %s 清单未找到", name)
            return False

        # 通过 importlib 加载插件模块
        plugin_dir = self._plugins_dir / name
        if not plugin_dir.is_dir():
            logger.warning("插件目录不存在: %s", plugin_dir)
            return False

        # 将插件目录加入 sys.path 以便 importlib 找到模块
        plugin_dir_str = str(plugin_dir)
        if plugin_dir_str not in sys.path:
            sys.path.insert(0, plugin_dir_str)

        # 清理 sys.modules 中可能残留的旧模块（避免测试间相互污染）
        # entry_point 通常是 "plugin"，多个插件用同名模块时必须清理
        if manifest.entry_point in sys.modules:
            del sys.modules[manifest.entry_point]

        try:
            module = importlib.import_module(manifest.entry_point)
            # 模块应导出 Plugin 类（约定名称：Plugin）
            plugin_cls = getattr(module, "Plugin", None)
            if plugin_cls is None:
                logger.warning("插件 %s 模块未导出 Plugin 类", name)
                return False
            plugin_instance = plugin_cls()
            plugin_instance.manifest = manifest
            # 调用 on_load 注入 context
            plugin_instance.on_load(self._context)
            self._loaded[name] = plugin_instance
            logger.info("插件 %s v%s 加载成功", name, manifest.version)
            return True
        except (ImportError, SyntaxError, AttributeError, TypeError,
                RuntimeError, ValueError) as e:
            logger.warning("插件 %s 加载失败: %s", name, e, exc_info=True)
            # 失败时清理 sys.modules 中的残留模块
            sys.modules.pop(manifest.entry_point, None)
            return False
        except Exception as e:
            # 兜底：插件 on_load 抛任意异常都不应崩溃主进程
            logger.warning("插件 %s on_load 异常: %s", name, e, exc_info=True)
            sys.modules.pop(manifest.entry_point, None)
            return False

    def unload(self, name: str) -> bool:
        """卸载插件

        Args:
            name: 插件名

        Returns:
            True 表示卸载成功；False 表示插件未加载或 on_unload 失败
        """
        plugin = self._loaded.get(name)
        if plugin is None:
            logger.warning("插件 %s 未加载，无法卸载", name)
            return False
        try:
            plugin.on_unload()
        except Exception as e:
            logger.warning("插件 %s on_unload 异常: %s", name, e, exc_info=True)
            # 仍然从已加载列表中移除
        del self._loaded[name]
        logger.info("插件 %s 已卸载", name)
        return True

    def get_plugin(self, name: str) -> Optional[IronPlugin]:
        """获取已加载的插件实例"""
        return self._loaded.get(name)

    def get_all_tools(self) -> list:
        """聚合所有已加载插件的工具"""
        tools = []
        for plugin in self._loaded.values():
            try:
                plugin_tools = plugin.get_tools()
                if plugin_tools:
                    tools.extend(plugin_tools)
            except Exception as e:
                logger.warning("插件 %s get_tools 异常: %s",
                               getattr(plugin.manifest, "name", "?"), e)
        return tools

    def get_all_skills(self) -> list:
        """聚合所有已加载插件的 Skill"""
        skills = []
        for plugin in self._loaded.values():
            try:
                plugin_skills = plugin.get_skills()
                if plugin_skills:
                    skills.extend(plugin_skills)
            except Exception as e:
                logger.warning("插件 %s get_skills 异常: %s",
                               getattr(plugin.manifest, "name", "?"), e)
        return skills

    def list_loaded(self) -> list[PluginManifest]:
        """列出已加载插件的清单"""
        return [p.manifest for p in self._loaded.values()]

    def is_loaded(self, name: str) -> bool:
        """检查插件是否已加载"""
        return name in self._loaded
