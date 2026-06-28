"""插件命令分组 — /plugin

子命令：
- /plugin list              列出已加载插件
- /plugin search <keyword>  搜索本地可用插件
- /plugin install <name>    加载本地插件
- /plugin remove <name>     卸载插件
- /plugin info <name>       显示插件详情

设计原则：
- 插件系统默认禁用（features.plugins=False），/plugin 命令仍可访问，但加载时提示需启用
- 命令失败不崩溃主进程，所有异常捕获后打印简短错误
"""
from pathlib import Path

from iron.cli.theme import Symbols
from rich.console import Console


def _get_plugin_manager(ctx: dict):
    """从 ctx 获取或创建 PluginManager

    若 ctx 中无 plugin_manager，返回 None（插件系统未初始化）
    """
    return ctx.get("plugin_manager")


def _get_plugins_dir(ctx: dict) -> Path:
    """获取插件目录（.iron-agent/plugins/）"""
    project_root = Path(ctx.get("project_root", "."))
    return project_root / ".iron-agent" / "plugins"


def handle_plugin_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理 /plugin 命令，返回 True 表示已处理

    ctx 包含: console, project_root, plugin_manager 等
    """
    console: Console = ctx.get("console") or Console()

    if cmd != "/plugin":
        return False

    parts = args.split(None, 1) if args else []
    subcmd = parts[0] if parts else "list"
    subarg = parts[1] if len(parts) > 1 else ""

    if subcmd == "list":
        _cmd_list(console, ctx)
    elif subcmd == "search":
        _cmd_search(console, ctx, subarg)
    elif subcmd in ("install", "enable"):
        _cmd_install(console, ctx, subarg)
    elif subcmd in ("remove", "disable"):
        _cmd_remove(console, ctx, subarg)
    elif subcmd == "info":
        _cmd_info(console, ctx, subarg)
    else:
        _show_usage(console)
    return True


def _show_usage(console: Console) -> None:
    """显示 /plugin 用法"""
    console.print(f"\n  {Symbols.WRENCH} /plugin 子命令\n")
    console.print("    list              列出已加载插件")
    console.print("    search <keyword>  搜索本地可用插件")
    console.print("    install <name>    加载本地插件")
    console.print("    remove <name>     卸载插件")
    console.print("    info <name>       显示插件详情\n")


def _cmd_list(console: Console, ctx: dict) -> None:
    """列出已加载插件"""
    mgr = _get_plugin_manager(ctx)
    if mgr is None:
        console.print(f"\n  {Symbols.WARN} 插件系统未初始化（features.plugins=False）\n",
                       style="yellow")
        return
    loaded = mgr.list_loaded()
    if not loaded:
        console.print(f"\n  {Symbols.INFO} 暂无已加载插件\n", style="cyan")
        return
    console.print(f"\n  {Symbols.CHECK} 已加载插件 ({len(loaded)} 个)\n")
    for m in loaded:
        console.print(f"    {Symbols.WRENCH}  [bold]{m.name}[/bold] v{m.version} — {m.description}")
    console.print()


def _cmd_search(console: Console, ctx: dict, keyword: str = "") -> None:
    """搜索本地可用插件"""
    mgr = _get_plugin_manager(ctx)
    if mgr is None:
        console.print(f"\n  {Symbols.WARN} 插件系统未初始化\n", style="yellow")
        return
    manifests = mgr.discover()
    if keyword:
        manifests = [m for m in manifests if keyword.lower() in m.name.lower()
                     or keyword.lower() in m.description.lower()]
    if not manifests:
        console.print(f"\n  {Symbols.INFO} 未找到可用插件\n", style="cyan")
        return
    console.print(f"\n  {Symbols.FOLDER} 本地可用插件 ({len(manifests)} 个)\n")
    for m in manifests:
        loaded = "✓" if mgr.is_loaded(m.name) else " "
        console.print(f"    [{loaded}] [bold]{m.name}[/bold] v{m.version} — {m.description}")
    console.print()


def _cmd_install(console: Console, ctx: dict, name: str) -> None:
    """加载本地插件"""
    if not name:
        console.print(f"\n  {Symbols.WARN} 用法: /plugin install <name>\n", style="yellow")
        return
    mgr = _get_plugin_manager(ctx)
    if mgr is None:
        console.print(f"\n  {Symbols.WARN} 插件系统未初始化\n", style="yellow")
        return
    if mgr.load(name):
        console.print(f"\n  {Symbols.CHECK} 插件 {name} 加载成功\n", style="green")
    else:
        console.print(f"\n  {Symbols.CROSS} 插件 {name} 加载失败（查看日志）\n", style="red")


def _cmd_remove(console: Console, ctx: dict, name: str) -> None:
    """卸载插件"""
    if not name:
        console.print(f"\n  {Symbols.WARN} 用法: /plugin remove <name>\n", style="yellow")
        return
    mgr = _get_plugin_manager(ctx)
    if mgr is None:
        console.print(f"\n  {Symbols.WARN} 插件系统未初始化\n", style="yellow")
        return
    if mgr.unload(name):
        console.print(f"\n  {Symbols.CHECK} 插件 {name} 已卸载\n", style="green")
    else:
        console.print(f"\n  {Symbols.CROSS} 插件 {name} 卸载失败\n", style="red")


def _cmd_info(console: Console, ctx: dict, name: str) -> None:
    """显示插件详情"""
    if not name:
        console.print(f"\n  {Symbols.WARN} 用法: /plugin info <name>\n", style="yellow")
        return
    mgr = _get_plugin_manager(ctx)
    if mgr is None:
        console.print(f"\n  {Symbols.WARN} 插件系统未初始化\n", style="yellow")
        return
    # 优先从已加载插件获取详情
    plugin = mgr.get_plugin(name)
    if plugin is not None:
        m = plugin.manifest
    else:
        # 从已 discover 的清单中查找
        if name not in mgr._manifests:
            mgr.discover()
        m = mgr._manifests.get(name)
    if m is None:
        console.print(f"\n  {Symbols.WARN} 插件 {name} 不存在\n", style="yellow")
        return
    loaded = "已加载" if mgr.is_loaded(name) else "未加载"
    console.print(f"\n  {Symbols.WRENCH}  [bold]{m.name}[/bold] v{m.version} ({loaded})\n")
    console.print(f"    描述: {m.description}")
    console.print(f"    作者: {m.author or '未指定'}")
    console.print(f"    主页: {m.homepage or '未指定'}")
    console.print(f"    最低 Iron 版本: {m.min_iron_version}")
    console.print(f"    权限: {', '.join(m.permissions) if m.permissions else '无'}")
    console.print(f"    入口: {m.entry_point}")
    console.print()
