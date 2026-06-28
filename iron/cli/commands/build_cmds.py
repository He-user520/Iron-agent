"""构建相关命令分组 — /code /check /build /flash /monitor /verify /explore

从 main.py 提取的构建/编译/烧录/验证相关命令处理逻辑，保持功能完全一致。
"""
from pathlib import Path

from iron.cli.theme import Symbols
from iron.cli import ui
from iron.agent.engine import TaskAgent, VerifyAgent
from iron.cli.main import (
    _run_agent, _cleanup_engine_mcp, _ThinkingSpinner, _inject_cli_event_to_session,
)


def handle_build_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理构建命令，返回 True 表示已处理

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
    session = ctx["session"]
    loop = ctx["loop"]

    if cmd == "/code":
        # /code 后面跟描述，直接作为输入
        if args:
            # 创建新 engine 前清理旧 engine 的 MCP 子进程，避免泄漏
            # 传入旧 engine 的 conversation，保持多轮对话上下文
            _prior_conv = last_engine.conversation if last_engine else None
            _cleanup_engine_mcp(last_engine)
            ctx["last_engine"] = loop.run_until_complete(
                _run_agent(console, llm, prompt_builder, skills, config, session, args,
                           prior_conversation=_prior_conv)
            )
        else:
            console.print(f"  用法: /code <需求描述>", style="yellow")
        return True

    elif cmd == "/check":
        # 运行 EmbedGuard 静态分析
        from iron.integrations.embedguard_bridge import analyze_paths
        check_paths = args.split() if args else ["src/"]
        console.print(f"\n  {Symbols.SHIELD} EmbedGuard 静态分析")
        console.print(f"    扫描路径: {', '.join(check_paths)}\n")
        spinner = _ThinkingSpinner(console)
        spinner.start("正在分析...")
        try:
            findings = analyze_paths(check_paths, config.project.mcu)
            if findings:
                ui.show_findings(console, findings)
            else:
                console.print(f"  {Symbols.CHECK} 未发现问题，代码通过静态分析！\n", style="green")
        except ImportError:
            console.print(f"  {Symbols.WARN} EmbedGuard 未安装\n", style="yellow")
        except (TypeError, OSError, RuntimeError) as e:
            ui.show_error(console, f"分析失败: {e}")
        finally:
            spinner.stop()
        return True

    elif cmd == "/build":
        # 调用 EmbedForge 编译工具链（不可用时自动 fallback 到 pio/make/cmake）
        from iron.integrations.embedforge_bridge import compile_project
        spinner = _ThinkingSpinner(console)
        spinner.start("正在编译...")
        try:
            result = compile_project(str(project_root))
            tool_name = result.get("tool", "未知工具")
            if result.get("success"):
                console.print(f"  {Symbols.CHECK} 编译成功（{tool_name}）", style="green")
                if result.get("flash_usage"):
                    console.print(f"    Flash: {result['flash_usage']}  RAM: {result['ram_usage']}", style="dim")
                # 显示部分编译输出
                output = result.get("output", "")
                if output:
                    for line in output.strip().split("\n")[-5:]:
                        console.print(f"    {line}", style="dim")
            else:
                # 给出可操作的错误提示
                console.print(f"  {Symbols.CROSS} 编译失败（{tool_name}）", style="red")
                console.print(f"    {result.get('output', '未知错误')}", style="dim red")
                hint = result.get("hint")
                if hint:
                    console.print(f"  {Symbols.INFO} 建议:", style="cyan")
                    for hint_line in hint.split("\n"):
                        if hint_line.strip():
                            console.print(f"    {hint_line.strip()}", style="dim cyan")
            # 把编译结果注入 session，让 AI 能识别
            # 用户反馈：选择编译后问 AI "什么意思"，AI 没有编译结果的记忆
            _inject_cli_event_to_session(
                session,
                event_type="build",
                summary="编译成功" if result.get("success") else "编译失败",
                details={
                    "tool": result.get("tool", ""),
                    "output": (result.get("output", "") or "")[-2000:],  # 截断避免上下文爆炸
                    "error": result.get("error", ""),
                    "hint": result.get("hint", ""),
                },
            )
        except (AttributeError, TypeError, OSError) as e:
            console.print(f"  {Symbols.CROSS} 编译调用失败: {e}", style="red")
            _inject_cli_event_to_session(
                session, event_type="build", summary=f"编译异常: {e}", details={},
            )
        finally:
            spinner.stop()
        return True

    elif cmd == "/flash":
        # 烧录固件
        from iron.integrations.embedforge_bridge import flash_firmware, list_probes
        # 查找固件
        firmware = args.strip() if args else ""
        if not firmware:
            # 自动查找
            for pattern in [".pio/build/*/firmware.bin", "build/*.bin", "*.bin"]:
                matches = list(Path(str(project_root)).glob(pattern))
                if matches:
                    firmware = str(matches[0])
                    break
        if not firmware:
            console.print(f"  {Symbols.WARN} 未找到固件文件，用法: /flash <firmware.bin>", style="yellow")
        else:
            console.print(f"\n  {Symbols.BOLT} 烧录固件: {firmware}")
            probes = list_probes()
            if probes:
                console.print(f"    可用探针: {', '.join(probes)}")
            spinner = _ThinkingSpinner(console)
            spinner.start("正在烧录...")
            try:
                result = flash_firmware(firmware)
            except (AttributeError, TypeError, OSError) as e:
                result = {"success": False, "error": str(e)}
            finally:
                spinner.stop()
            if result.get("success"):
                console.print(f"  {Symbols.CHECK} 烧录成功\n", style="green")
            else:
                console.print(f"  {Symbols.CROSS} 烧录失败: {result.get('output', result.get('error', '未知错误'))}\n", style="red")
        return True

    elif cmd == "/monitor":
        # 串口监视器
        from iron.integrations.embedforge_bridge import list_serial_ports
        ports = list_serial_ports()
        if not ports:
            console.print(f"\n  {Symbols.WARN} 未发现可用串口", style="yellow")
            console.print(f"    请确认设备已连接并安装驱动\n")
        else:
            console.print(f"\n  {Symbols.SERIAL} 可用串口:")
            for p in ports:
                console.print(f"    {p}")
            console.print(f"\n  使用方法: 在终端运行 `pio device monitor -p <端口> -b 115200`")
            console.print(f"  或在 iron 中说\"打开串口监视器\"\n")
        return True

    elif cmd == "/verify":
        # P3-4: /verify [target] - 验证代码质量（使用 VerifyAgent）
        # 自动跑 EmbedGuard 静态分析 + LSP 诊断 + 编译检查，
        # 给出问题列表和整体评估（通过/警告/失败）
        args_str = args or "src/"
        _prior_conv = last_engine.conversation if last_engine else None
        _cleanup_engine_mcp(last_engine)
        # 用 VerifyAgent 执行完整验证流程（内部复用 process() ReAct 循环）
        # 复用 _run_agent 的事件处理逻辑，获得实时进度显示
        _verify_prompt = (
            f"请验证 {args_str} 目录的代码质量。按以下步骤执行：\n"
            "1. 用 embed_lint 进行静态分析\n"
            "2. 检查 LSP 诊断（如果可用）\n"
            "3. 运行编译检查（platformio run，只读不烧录）\n"
            "4. 给出问题列表（按严重度排序）和整体评估（通过/警告/失败）"
        )
        ctx["last_engine"] = loop.run_until_complete(_run_agent(
            console, llm, prompt_builder, skills, config, session, _verify_prompt,
            prior_conversation=_prior_conv, engine_class=VerifyAgent,
        ))
        return True

    elif cmd == "/explore":
        # P1-4: /explore <query> - 只读探索代码库（使用 TaskAgent）
        # AI 只能用只读工具（read_file/search_code/find_files/web_search），
        # 不能修改文件、不能编译/烧录，用于安全的代码库探索和方案规划
        if args:
            _prior_conv = last_engine.conversation if last_engine else None
            _cleanup_engine_mcp(last_engine)
            ctx["last_engine"] = loop.run_until_complete(_run_agent(
                console, llm, prompt_builder, skills, config, session, args,
                prior_conversation=_prior_conv, engine_class=TaskAgent,
            ))
        else:
            console.print(f"  用法: /explore <探索需求>", style="yellow")
        return True

    # 未匹配返回 False
    return False
