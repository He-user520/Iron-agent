"""/git 命令分组 — 直接在对话中执行 Git 操作（v4.0 Track 5）

子命令：
- /git                  查看状态（默认 = status）
- /git status           查看工作区状态
- /git diff [--staged] [path]  查看 diff
- /git log [N]          查看最近 N 条提交（默认 10）
- /git add <file...>    暂存文件
- /git commit -m "msg"  提交（不经过权限回调，因为用户已主动发起命令）

设计原则：
- 委托给 git_tools 中的工具类，避免逻辑重复
- 工具是 async，命令处理用 asyncio.run 同步调用
- 命令失败不崩溃主进程
"""
import asyncio

from iron.cli.theme import Symbols
from rich.console import Console


def _run_git_tool(tool_cls, tool_args: dict, project_root: str) -> dict:
    """同步包装：用 asyncio.run 执行 async 工具"""
    tool = tool_cls()
    try:
        return asyncio.run(tool.execute(tool_args, {"project_dir": project_root}))
    except RuntimeError:
        # 已在 event loop 中（理论上 CLI 不会出现，但兜底）
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 用 ensure_future 调度但无法等待结果，返回降级错误
            return {"success": False, "error": "无法在运行中的事件循环里同步执行 git 工具",
                    "output": ""}
        return loop.run_until_complete(
            tool.execute(tool_args, {"project_dir": project_root})
        )


def handle_git_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理 /git 命令，返回 True 表示已处理

    ctx 包含: console, project_root 等
    """
    if cmd != "/git":
        return False
    console: Console = ctx.get("console") or Console()
    project_root = str(ctx.get("project_root", "."))

    # 解析子命令
    parts = args.split(None, 1) if args else []
    subcmd = parts[0] if parts else "status"
    subarg = parts[1] if len(parts) > 1 else ""

    # 延迟导入避免循环依赖
    from iron.tools.git_tools import (
        GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool,
    )

    if subcmd == "status":
        tool_cls, tool_args = GitStatusTool, {}
    elif subcmd == "diff":
        tool_cls = GitDiffTool
        tool_args = {
            "staged": "--staged" in subarg or "--cached" in subarg,
        }
        # 提取路径参数（去掉 --staged/--cached 标记）
        path_tokens = [t for t in subarg.split()
                       if t not in ("--staged", "--cached") and t.strip()]
        if path_tokens:
            tool_args["path"] = path_tokens[0]
    elif subcmd == "log":
        tool_cls = GitLogTool
        tool_args = {}
        # 解析数字 limit
        if subarg.strip().isdigit():
            tool_args["limit"] = int(subarg.strip())
    elif subcmd == "add":
        tool_cls = GitAddTool
        paths = subarg.split()
        if not paths:
            console.print(f"\n  {Symbols.WARN}  用法: /git add <file...>\n",
                          style="yellow")
            return True
        tool_args = {"paths": paths}
    elif subcmd == "commit":
        tool_cls = GitCommitTool
        # 解析 -m "msg" 或 -m msg
        msg = ""
        if subarg.startswith("-m"):
            rest = subarg[2:].strip()
            # 支持引号包裹的消息
            if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
                msg = rest[1:-1]
            elif rest.startswith("'") and rest.endswith("'") and len(rest) >= 2:
                msg = rest[1:-1]
            else:
                msg = rest
        if not msg:
            console.print(f"\n  {Symbols.WARN}  用法: /git commit -m \"提交信息\"\n",
                          style="yellow")
            return True
        tool_args = {"message": msg}
    else:
        console.print(f"\n  {Symbols.WARN} 未知子命令: {subcmd}\n", style="yellow")
        console.print("  可用: status / diff / log / add / commit\n")
        return True

    result = _run_git_tool(tool_cls, tool_args, project_root)
    if result.get("success"):
        console.print(f"\n  {Symbols.CHECK} {result.get('output', '')}\n",
                      style="green")
    else:
        console.print(f"\n  {Symbols.CROSS} {result.get('error', '失败')}\n",
                      style="red")
    return True
