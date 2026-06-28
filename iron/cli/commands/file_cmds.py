"""文件操作命令分组 — /read /write /edit /delete /files /undo

从 main.py 提取的文件相关命令处理逻辑，保持功能完全一致。
"""
import shlex

import click

from iron.cli.theme import Symbols
from iron.cli import ui
from iron.agent.engine import TaskAgent
from iron.cli.main import _validate_project_path, _do_undo


def handle_file_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理文件命令，返回 True 表示已处理

    ctx 包含: console, config, project_root, llm, prompt_builder, skills,
              last_engine, session, loop 等
    """
    console = ctx["console"]
    project_root = ctx["project_root"]
    llm = ctx["llm"]
    prompt_builder = ctx["prompt_builder"]
    skills = ctx["skills"]
    config = ctx["config"]
    last_engine = ctx["last_engine"]
    loop = ctx["loop"]

    if cmd == "/read":
        # /read <file> - 读取并显示文件内容
        # 用独立 engine 避免污染 agent conversation（read_file 会在 conversation
        # 中追加 tool 消息，影响后续 LLM 上下文）
        # P1-4: 使用 TaskAgent（只读，更安全），避免误触发写工具
        if args:
            _read_engine = TaskAgent(llm=llm, prompt_builder=prompt_builder,
                                      skills=skills, config=config)
            loop.run_until_complete(_read_engine.read_file(args))
        else:
            console.print(f"  用法: /read <文件路径>", style="yellow")
        return True

    elif cmd == "/write":
        # /write <file> <content> - 写入文件
        if args and " " in args:
            file_path, content = args.split(" ", 1)
            try:
                full_path = _validate_project_path(file_path, project_root, allow_create=True)
            except ValueError as e:
                console.print(f"  ⚠ {e}", style="red")
                return True
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            console.print(f"  {Symbols.CHECK} 已写入 [bold]{file_path}[/bold]", style="green")
        else:
            console.print(f"  用法: /write <文件路径> <内容>", style="yellow")
        return True

    elif cmd == "/edit":
        # /edit <file> <old> <new> - 编辑文件
        # 用 shlex.split 支持含空格的参数（引号包裹）
        if args:
            try:
                parts = shlex.split(args)
            except ValueError as e:
                console.print(f"  ⚠ 参数解析失败: {e}", style="red")
                parts = []
            if len(parts) >= 3:
                file_path, old_text, new_text = parts[0], parts[1], parts[2]
                try:
                    full_path = _validate_project_path(file_path, project_root, allow_create=False)
                except ValueError as e:
                    console.print(f"  ⚠ {e}", style="red")
                    return True
                if full_path.exists():
                    try:
                        content = full_path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        content = full_path.read_text(encoding="gbk", errors="replace")
                    if old_text in content:
                        new_content = content.replace(old_text, new_text)
                        full_path.write_text(new_content, encoding="utf-8")
                        console.print(f"  {Symbols.CHECK} 已编辑 [bold]{file_path}[/bold]", style="green")
                    else:
                        console.print(f"  {Symbols.WARN} 未找到要替换的内容", style="yellow")
                else:
                    console.print(f"  {Symbols.WARN} 文件不存在: {file_path}", style="yellow")
            else:
                console.print(f"  用法: /edit <文件路径> <旧内容> <新内容>（含空格请用引号包裹）", style="yellow")
        else:
            console.print(f"  用法: /edit <文件路径> <旧内容> <新内容>（含空格请用引号包裹）", style="yellow")
        return True

    elif cmd == "/delete":
        # /delete <file> - 删除文件
        if args:
            try:
                full_path = _validate_project_path(args, project_root, allow_create=False)
            except ValueError as e:
                console.print(f"  ⚠ {e}", style="red")
                return True
            if full_path.exists():
                confirm = click.confirm(f"确认删除 {full_path}?", default=False)
                if not confirm:
                    console.print("  已取消", style="dim")
                    return True
                full_path.unlink()
                console.print(f"  {Symbols.CHECK} 已删除 [bold]{args}[/bold]", style="green")
            else:
                console.print(f"  {Symbols.WARN} 文件不存在: {args}", style="yellow")
        else:
            console.print(f"  用法: /delete <文件路径>", style="yellow")
        return True

    elif cmd == "/files":
        # 浏览项目文件
        ui.show_file_tree(console, str(project_root),
                          changed_files=[r["path"] for r in (last_engine._change_history if last_engine else [])])
        return True

    elif cmd == "/undo":
        if last_engine is not None:
            _do_undo(console, last_engine)
        else:
            console.print(f"  {Symbols.WARN} 没有可撤销的操作", style="dim yellow")
        return True

    # 未匹配返回 False
    return False
