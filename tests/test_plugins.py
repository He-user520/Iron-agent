"""插件系统测试 — PluginManager / PluginContext / PluginManifest / IronPlugin

测试覆盖：
- PluginManifest 数据类
- IronPlugin 基类
- PluginContext 权限校验和文件操作
- PluginManager discover/load/unload/get_all_tools
- /plugin 命令分发
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from iron.plugins import (
    IronPlugin, PluginManifest, PluginContext, PluginManager,
)
from iron.plugins.base import IronPlugin as BaseIronPlugin
from iron.plugins.context import (
    PERM_FILE_READ, PERM_FILE_WRITE, PERM_RUN_COMMAND, PERM_NETWORK,
)
from iron.cli.commands.plugin_cmds import handle_plugin_commands


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture
def project_root(tmp_path) -> Path:
    """临时项目根目录"""
    return tmp_path


@pytest.fixture
def plugins_dir(project_root) -> Path:
    """插件目录（.iron-agent/plugins/）"""
    pdir = project_root / ".iron-agent" / "plugins"
    pdir.mkdir(parents=True)
    return pdir


@pytest.fixture
def context(project_root):
    """PluginContext 实例（带全部权限）"""
    return PluginContext(
        project_root=str(project_root),
        permissions=[PERM_FILE_READ, PERM_FILE_WRITE, PERM_RUN_COMMAND, PERM_NETWORK],
    )


@pytest.fixture
def manager(plugins_dir, context):
    """PluginManager 实例"""
    return PluginManager(str(plugins_dir), context)


def _create_test_plugin(plugins_dir: Path, name: str = "test-plugin",
                        description: str = "测试插件",
                        permissions: list = None):
    """在 plugins_dir 下创建一个测试插件目录"""
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # plugin.json
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": description,
        "author": "test",
        "permissions": permissions or ["file_read"],
        "entry_point": "plugin",
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    # plugin.py — 导出 Plugin 类
    (plugin_dir / "plugin.py").write_text(
        'from iron.plugins import IronPlugin\n'
        '\n'
        'class Plugin(IronPlugin):\n'
        '    def on_load(self, context):\n'
        '        self.ctx = context\n'
        '    def get_tools(self):\n'
        '        return []\n',
        encoding="utf-8",
    )
    return plugin_dir


# ── 1. PluginManifest 数据类 ────────────────────────────────────

class TestPluginManifest:
    """PluginManifest 数据类测试"""

    def test_create_minimal_manifest(self):
        """创建最小清单（仅必填字段）"""
        m = PluginManifest(name="test", version="1.0.0", description="测试")
        assert m.name == "test"
        assert m.version == "1.0.0"
        assert m.description == "测试"
        assert m.author == ""
        assert m.min_iron_version == "2.8.0"
        assert m.permissions == []
        assert m.entry_point == "plugin"

    def test_create_full_manifest(self):
        """创建完整清单"""
        m = PluginManifest(
            name="stm32-helper",
            version="2.1.0",
            description="STM32 辅助工具",
            author="iron",
            homepage="https://example.com",
            min_iron_version="3.0.0",
            permissions=["file_read", "file_write"],
            entry_point="main",
        )
        assert m.author == "iron"
        assert m.homepage == "https://example.com"
        assert m.min_iron_version == "3.0.0"
        assert m.permissions == ["file_read", "file_write"]
        assert m.entry_point == "main"

    def test_default_permissions_is_empty_list(self):
        """permissions 默认为空列表"""
        m1 = PluginManifest(name="a", version="1.0", description="x")
        m2 = PluginManifest(name="b", version="1.0", description="y")
        # 验证默认值不共享（field(default_factory=list)）
        m1.permissions.append("file_read")
        assert m2.permissions == []


# ── 2. IronPlugin 基类 ──────────────────────────────────────────

class TestIronPlugin:
    """IronPlugin 基类测试"""

    def test_on_load_raises_not_implemented(self):
        """基类 on_load 抛 NotImplementedError"""
        plugin = IronPlugin()
        with pytest.raises(NotImplementedError):
            plugin.on_load(None)

    def test_on_unload_default_no_op(self):
        """基类 on_unload 默认空实现"""
        plugin = IronPlugin()
        # 不抛异常即视为通过
        plugin.on_unload()

    def test_get_tools_default_empty(self):
        """基类 get_tools 默认返回空列表"""
        plugin = IronPlugin()
        assert plugin.get_tools() == []

    def test_get_skills_default_empty(self):
        plugin = IronPlugin()
        assert plugin.get_skills() == []

    def test_get_hooks_default_empty(self):
        plugin = IronPlugin()
        assert plugin.get_hooks() == []

    def test_subclass_implements_on_load(self):
        """子类实现 on_load 后可正常调用"""
        class MyPlugin(IronPlugin):
            def on_load(self, context):
                self.loaded = True

        plugin = MyPlugin()
        plugin.on_load(MagicMock())
        assert plugin.loaded is True


# ── 3. PluginContext ────────────────────────────────────────────

class TestPluginContext:
    """PluginContext 权限和文件操作测试"""

    def test_init_default_logger(self, project_root):
        """不传 logger 时使用默认 logger"""
        ctx = PluginContext(project_root=str(project_root))
        assert ctx.logger is not None
        assert ctx.permissions == []

    def test_init_default_permissions_empty(self, project_root):
        ctx = PluginContext(project_root=str(project_root))
        assert ctx.permissions == []

    def test_read_file_without_permission_raises(self, project_root, tmp_path):
        """无 file_read 权限时 read_file 抛 PermissionError"""
        f = tmp_path / "test.txt"
        f.write_text("hello", encoding="utf-8")
        ctx = PluginContext(project_root=str(project_root), permissions=[])
        with pytest.raises(PermissionError):
            ctx.read_file("test.txt")

    def test_write_file_without_permission_raises(self, project_root):
        """无 file_write 权限时 write_file 抛 PermissionError"""
        ctx = PluginContext(project_root=str(project_root), permissions=[])
        with pytest.raises(PermissionError):
            ctx.write_file("test.txt", "content")

    def test_run_command_without_permission_raises(self, project_root):
        """无 run_command 权限时 run_command 抛 PermissionError"""
        ctx = PluginContext(project_root=str(project_root), permissions=[])
        with pytest.raises(PermissionError):
            ctx.run_command("echo hi")

    def test_read_file_with_permission(self, project_root):
        """有 file_read 权限时可读取项目内文件"""
        f = project_root / "data.txt"
        f.write_text("hello world", encoding="utf-8")
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_FILE_READ],
        )
        content = ctx.read_file("data.txt")
        assert content == "hello world"

    def test_write_file_with_permission(self, project_root):
        """有 file_write 权限时可写入项目内文件"""
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_FILE_WRITE],
        )
        assert ctx.write_file("output.txt", "content") is True
        assert (project_root / "output.txt").read_text(encoding="utf-8") == "content"

    def test_read_file_path_traversal_blocked(self, project_root):
        """路径越界访问被拒绝"""
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_FILE_READ],
        )
        with pytest.raises(ValueError):
            ctx.read_file("../etc/passwd")

    def test_write_file_path_traversal_blocked(self, project_root):
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_FILE_WRITE],
        )
        with pytest.raises(ValueError):
            ctx.write_file("../../etc/cron.d/evil", "content")

    def test_run_command_with_permission(self, project_root):
        """有 run_command 权限时执行命令"""
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_RUN_COMMAND],
        )
        result = ctx.run_command("echo hello")
        assert result["success"] is True
        assert "hello" in result["stdout"]

    def test_run_command_timeout(self, project_root):
        """命令超时返回错误"""
        ctx = PluginContext(
            project_root=str(project_root),
            permissions=[PERM_RUN_COMMAND],
        )
        result = ctx.run_command("ping -n 10 127.0.0.1", timeout=1)
        assert result["success"] is False
        assert "超时" in result["stderr"]

    def test_subscribe_event_without_bus(self, project_root):
        """无 event_bus 时 subscribe_event 不崩溃"""
        ctx = PluginContext(project_root=str(project_root))
        # 不抛异常即视为通过
        ctx.subscribe_event("test_event", lambda x: None)


# ── 4. PluginManager discover ──────────────────────────────────

class TestPluginManagerDiscover:
    """PluginManager discover 测试"""

    def test_discover_empty_dir(self, manager):
        """空目录返回空列表"""
        assert manager.discover() == []

    def test_discover_finds_plugin(self, manager, plugins_dir):
        """发现插件目录中的插件"""
        _create_test_plugin(plugins_dir, "my-plugin", "我的插件")
        manifests = manager.discover()
        assert len(manifests) == 1
        assert manifests[0].name == "my-plugin"
        assert manifests[0].version == "1.0.0"
        assert manifests[0].description == "我的插件"

    def test_discover_multiple_plugins(self, manager, plugins_dir):
        """发现多个插件"""
        _create_test_plugin(plugins_dir, "plugin-a", "插件A")
        _create_test_plugin(plugins_dir, "plugin-b", "插件B")
        manifests = manager.discover()
        names = [m.name for m in manifests]
        assert "plugin-a" in names
        assert "plugin-b" in names

    def test_discover_skips_directories_without_manifest(self, manager, plugins_dir):
        """跳过无 plugin.json 的目录"""
        (plugins_dir / "no-manifest").mkdir()
        _create_test_plugin(plugins_dir, "has-manifest", "有清单")
        manifests = manager.discover()
        assert len(manifests) == 1
        assert manifests[0].name == "has-manifest"

    def test_discover_skips_hidden_directories(self, manager, plugins_dir):
        """跳过隐藏目录（以 . 或 _ 开头）"""
        (plugins_dir / ".hidden").mkdir()
        (plugins_dir / "_internal").mkdir()
        manifests = manager.discover()
        assert manifests == []

    def test_discover_handles_invalid_json(self, manager, plugins_dir):
        """损坏的 plugin.json 被跳过"""
        bad_dir = plugins_dir / "bad-plugin"
        bad_dir.mkdir()
        (bad_dir / "plugin.json").write_text("not json", encoding="utf-8")
        manifests = manager.discover()
        assert manifests == []

    def test_discover_nonexistent_dir(self, project_root):
        """不存在的插件目录返回空列表"""
        mgr = PluginManager(
            str(project_root / "nonexistent"),
            PluginContext(project_root=str(project_root)),
        )
        assert mgr.discover() == []


# ── 5. PluginManager load/unload ───────────────────────────────

class TestPluginManagerLoad:
    """PluginManager load/unload 测试"""

    def test_load_plugin_success(self, manager, plugins_dir):
        """成功加载插件"""
        _create_test_plugin(plugins_dir, "test-plugin", "测试")
        assert manager.load("test-plugin") is True
        assert manager.is_loaded("test-plugin") is True

    def test_load_already_loaded_returns_false(self, manager, plugins_dir):
        """重复加载返回 False"""
        _create_test_plugin(plugins_dir, "test-plugin")
        assert manager.load("test-plugin") is True
        assert manager.load("test-plugin") is False

    def test_load_nonexistent_plugin_returns_false(self, manager):
        """加载不存在的插件返回 False"""
        assert manager.load("nonexistent") is False

    def test_load_plugin_on_load_called(self, manager, plugins_dir):
        """on_load 被调用"""
        _create_test_plugin(plugins_dir, "test-plugin")
        manager.load("test-plugin")
        plugin = manager.get_plugin("test-plugin")
        assert plugin is not None
        assert hasattr(plugin, "ctx")
        assert plugin.ctx is manager._context

    def test_unload_plugin_success(self, manager, plugins_dir):
        """成功卸载插件"""
        _create_test_plugin(plugins_dir, "test-plugin")
        manager.load("test-plugin")
        assert manager.unload("test-plugin") is True
        assert not manager.is_loaded("test-plugin")

    def test_unload_nonexistent_returns_false(self, manager):
        """卸载未加载的插件返回 False"""
        assert manager.unload("nonexistent") is False

    def test_get_plugin_returns_none_if_not_loaded(self, manager):
        assert manager.get_plugin("nonexistent") is None

    def test_load_plugin_with_broken_module_returns_false(self, manager, plugins_dir):
        """插件模块加载失败返回 False"""
        plugin_dir = plugins_dir / "broken-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({
                "name": "broken-plugin",
                "version": "1.0.0",
                "description": "broken",
            }),
            encoding="utf-8",
        )
        # plugin.py 有语法错误
        (plugin_dir / "plugin.py").write_text(
            "this is not valid python!!!",
            encoding="utf-8",
        )
        assert manager.load("broken-plugin") is False

    def test_load_plugin_without_plugin_class_returns_false(self, manager, plugins_dir):
        """插件模块未导出 Plugin 类返回 False"""
        plugin_dir = plugins_dir / "no-class-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({
                "name": "no-class-plugin",
                "version": "1.0.0",
                "description": "no class",
            }),
            encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            "# no Plugin class here\nx = 1\n",
            encoding="utf-8",
        )
        assert manager.load("no-class-plugin") is False

    def test_load_plugin_on_load_exception_returns_false(self, manager, plugins_dir):
        """on_load 抛异常时加载失败但不崩溃"""
        plugin_dir = plugins_dir / "exception-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({
                "name": "exception-plugin",
                "version": "1.0.0",
                "description": "throws",
            }),
            encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            'from iron.plugins import IronPlugin\n'
            '\n'
            'class Plugin(IronPlugin):\n'
            '    def on_load(self, context):\n'
            '        raise RuntimeError("intentional failure")\n',
            encoding="utf-8",
        )
        assert manager.load("exception-plugin") is False


# ── 6. PluginManager 聚合方法 ──────────────────────────────────

class TestPluginManagerAggregation:
    """get_all_tools / get_all_skills / list_loaded 测试"""

    def test_get_all_tools_empty(self, manager):
        assert manager.get_all_tools() == []

    def test_get_all_tools_aggregates(self, manager, plugins_dir):
        """聚合多个插件的工具"""
        # 创建带工具的插件
        plugin_dir = plugins_dir / "tool-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "tool-plugin", "version": "1.0.0",
                        "description": "with tools"}),
            encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            'from iron.plugins import IronPlugin\n'
            'from iron.tools.base import BaseTool\n'
            '\n'
            'class MyTool(BaseTool):\n'
            '    @property\n'
            '    def name(self): return "my_tool"\n'
            '    @property\n'
            '    def schema(self): return {"type":"function","function":{"name":"my_tool","parameters":{"type":"object","properties":{}}}}\n'
            '    async def execute(self, args, context): return {"success": True}\n'
            '\n'
            'class Plugin(IronPlugin):\n'
            '    def on_load(self, context): pass\n'
            '    def get_tools(self): return [MyTool()]\n',
            encoding="utf-8",
        )
        manager.load("tool-plugin")
        tools = manager.get_all_tools()
        assert len(tools) == 1
        assert tools[0].name == "my_tool"

    def test_list_loaded_empty(self, manager):
        assert manager.list_loaded() == []

    def test_list_loaded_returns_manifests(self, manager, plugins_dir):
        _create_test_plugin(plugins_dir, "p1")
        _create_test_plugin(plugins_dir, "p2")
        manager.load("p1")
        manager.load("p2")
        loaded = manager.list_loaded()
        assert len(loaded) == 2
        names = [m.name for m in loaded]
        assert "p1" in names
        assert "p2" in names


# ── 7. /plugin 命令分发 ────────────────────────────────────────

class TestPluginCommands:
    """/plugin 命令测试"""

    @pytest.fixture
    def console(self):
        return Console(quiet=True)

    @pytest.fixture
    def ctx(self, project_root, plugins_dir, context, console):
        """命令 ctx"""
        mgr = PluginManager(str(plugins_dir), context)
        return {
            "console": console,
            "project_root": str(project_root),
            "plugin_manager": mgr,
        }

    def test_handle_unknown_command_returns_false(self, ctx):
        """非 /plugin 命令返回 False"""
        assert handle_plugin_commands("/help", "", ctx) is False

    def test_plugin_list_no_manager(self, project_root, console):
        """无 plugin_manager 时提示未初始化"""
        ctx = {"console": console, "project_root": str(project_root)}
        # 不崩溃即可
        assert handle_plugin_commands("/plugin", "list", ctx) is True

    def test_plugin_list_empty(self, ctx):
        """空插件列表"""
        assert handle_plugin_commands("/plugin", "list", ctx) is True

    def test_plugin_list_with_loaded(self, ctx):
        """列出已加载插件"""
        _create_test_plugin(Path(ctx["project_root"]) / ".iron-agent" / "plugins", "p1")
        ctx["plugin_manager"].load("p1")
        assert handle_plugin_commands("/plugin", "list", ctx) is True

    def test_plugin_search_no_results(self, ctx):
        assert handle_plugin_commands("/plugin", "search nonexistent", ctx) is True

    def test_plugin_search_with_keyword(self, ctx):
        _create_test_plugin(Path(ctx["project_root"]) / ".iron-agent" / "plugins",
                            "stm32-helper", "STM32 工具")
        assert handle_plugin_commands("/plugin", "search stm32", ctx) is True

    def test_plugin_install_success(self, ctx):
        _create_test_plugin(Path(ctx["project_root"]) / ".iron-agent" / "plugins", "p1")
        assert handle_plugin_commands("/plugin", "install p1", ctx) is True

    def test_plugin_install_no_name(self, ctx):
        assert handle_plugin_commands("/plugin", "install", ctx) is True

    def test_plugin_remove_success(self, ctx):
        plugins_dir = Path(ctx["project_root"]) / ".iron-agent" / "plugins"
        _create_test_plugin(plugins_dir, "p1")
        ctx["plugin_manager"].load("p1")
        assert handle_plugin_commands("/plugin", "remove p1", ctx) is True

    def test_plugin_info(self, ctx):
        plugins_dir = Path(ctx["project_root"]) / ".iron-agent" / "plugins"
        _create_test_plugin(plugins_dir, "p1", "测试插件")
        ctx["plugin_manager"].load("p1")
        assert handle_plugin_commands("/plugin", "info p1", ctx) is True

    def test_plugin_info_nonexistent(self, ctx):
        assert handle_plugin_commands("/plugin", "info nonexistent", ctx) is True

    def test_plugin_unknown_subcmd_shows_usage(self, ctx):
        assert handle_plugin_commands("/plugin", "unknownsubcmd", ctx) is True

    def test_plugin_no_args_shows_list(self, ctx):
        """无参数默认执行 list"""
        assert handle_plugin_commands("/plugin", "", ctx) is True
