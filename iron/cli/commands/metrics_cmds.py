"""/metrics 命令 — 显示会话指标（v4.0 Track 9）

用法:
    /metrics           显示所有指标（counters / gauges / timings）
    /metrics reset     重置所有指标

设计原则：
- 委托给 iron.utils.metrics 单例，避免逻辑重复
- 指标为空时给出友好提示，不打印空标题
- 命令失败不崩溃主进程
"""
from iron.cli.theme import Symbols
from rich.console import Console


def handle_metrics_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理 /metrics 命令，返回 True 表示已处理

    ctx 包含: console, project_root 等
    """
    if cmd != "/metrics":
        return False
    console: Console = ctx.get("console") or Console()

    # 子命令解析：/metrics reset
    sub = args.strip() if args else ""
    if sub == "reset":
        try:
            from iron.utils.metrics import reset
            reset()
            console.print(f"\n  {Symbols.CHECK} 指标已重置\n", style="green")
        except ImportError:
            console.print(f"\n  {Symbols.WARN} metrics 模块未加载\n",
                          style="yellow")
        return True

    # 默认：显示指标摘要
    try:
        from iron.utils.metrics import get_summary
        summary = get_summary()
    except ImportError:
        console.print(f"\n  {Symbols.WARN} metrics 模块未加载\n",
                      style="yellow")
        return True

    counters = summary.get("counters", {})
    gauges = summary.get("gauges", {})
    timings = summary.get("timings", {})

    # 全空时给出提示
    if not counters and not gauges and not timings:
        console.print(f"\n  {Symbols.INFO} 暂无指标数据（会话尚未产生工具调用或 LLM 请求）\n",
                      style="cyan")
        return True

    console.print(f"\n  {Symbols.WRENCH} 会话指标\n")

    if counters:
        console.print("  [bold]计数器 (counters):[/bold]")
        for k in sorted(counters.keys()):
            v = counters[k]
            # 整数计数器显示为整数
            display = int(v) if float(v).is_integer() else f"{v:.2f}"
            console.print(f"    {k}: {display}")
        console.print()

    if gauges:
        console.print("  [bold]仪表盘 (gauges):[/bold]")
        for k in sorted(gauges.keys()):
            v = gauges[k]
            display = int(v) if float(v).is_integer() else f"{v:.2f}"
            console.print(f"    {k}: {display}")
        console.print()

    if timings:
        console.print("  [bold]耗时 (timings):[/bold]")
        for k in sorted(timings.keys()):
            v = timings[k]
            console.print(
                f"    {k}: count={v['count']}, "
                f"avg={v['avg']:.3f}s, min={v['min']:.3f}s, max={v['max']:.3f}s"
            )
        console.print()

    return True
