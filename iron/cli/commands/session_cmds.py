"""会话管理命令分组 — /history /resume /compact /context /clear

从 main.py 提取的会话/上下文管理相关命令处理逻辑，保持功能完全一致。
"""
from iron.cli.theme import Symbols
from iron.cli import ui
from iron.agent.engine import AgentEngine
from iron.cli.main import (
    _show_history, _resume_session, _cleanup_engine_mcp,
)


def handle_session_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理会话命令，返回 True 表示已处理

    ctx 包含: console, config, project_root, llm, prompt_builder, skills,
              last_engine, session, loop, total_rules 等
    """
    console = ctx["console"]
    config = ctx["config"]
    project_root = ctx["project_root"]
    llm = ctx["llm"]
    prompt_builder = ctx["prompt_builder"]
    skills = ctx["skills"]
    last_engine = ctx["last_engine"]
    loop = ctx["loop"]
    total_rules = ctx["total_rules"]

    if cmd == "/clear":
        # 彻底清屏（含滚动缓冲区），避免 rich console.clear() 只清当前屏幕的问题
        from iron.cli.main import _clear_screen_full
        _clear_screen_full()
        # 显示当前 agent（无 engine 时也能显示）
        agent_mgr = ctx.get("agent_manager")
        _agent_name = agent_mgr.get_current_name() if agent_mgr else ""
        ui.show_status_bar(console, config.project.mcu, config.llm.model,
                           total_rules, config.project.build_system, agent=_agent_name)
        return True

    elif cmd == "/compact":
        # 手动触发上下文压缩
        # 用 loop.run_until_complete 复用会话事件循环，避免 httpx 客户端跨循环
        if last_engine:
            system = last_engine._build_system_prompt()
            old_count = len(last_engine.conversation)
            last_engine.conversation = loop.run_until_complete(
                last_engine._compactor.compact_if_needed(last_engine.conversation, system)
            )
            new_count = len(last_engine.conversation)
            console.print(f"  {Symbols.CHECK} 上下文已压缩: {old_count} → {new_count} 条消息", style="green")
        else:
            console.print("  没有活跃的会话", style="dim")
        return True

    elif cmd == "/context":
        # 显示上下文使用情况
        if last_engine:
            from iron.agent.memory import estimate_messages_tokens, estimate_tokens
            msg_tokens = estimate_messages_tokens(last_engine.conversation)
            system = last_engine._build_system_prompt()
            sys_tokens = estimate_tokens(system)
            total = msg_tokens + sys_tokens
            console.print(f"\n  {Symbols.BRAIN} 上下文使用情况")
            console.print(f"    消息: {len(last_engine.conversation)} 条, ~{msg_tokens} tokens")
            console.print(f"    系统提示: ~{sys_tokens} tokens")
            console.print(f"    总计: ~{total} tokens")
            # 用全局 agent_manager（即使无 engine 也能查）
            agent_mgr = ctx.get("agent_manager")
            if agent_mgr:
                agent = agent_mgr.get_current()
                console.print(f"    当前 Agent: {agent.name}")
            console.print()
        else:
            # 无 engine 时也能显示当前 agent
            agent_mgr = ctx.get("agent_manager")
            if agent_mgr:
                agent = agent_mgr.get_current()
                console.print(f"  {Symbols.BRAIN} 当前 Agent: {agent.name}", style="dim")
            else:
                console.print("  没有活跃的会话", style="dim")
        return True

    elif cmd == "/history":
        _show_history(project_root)
        return True

    elif cmd == "/resume":
        # /resume [session_id] — 恢复历史会话
        session_id = args.strip()
        resumed = _resume_session(project_root, session_id)
        if resumed:
            ctx["session"] = resumed
            session = resumed
            console.print(f"  {Symbols.CHECK} 已恢复会话（{len(session.messages)} 条消息）", style="green")
            # 创建新 engine 前清理旧 engine 的 MCP 子进程，避免资源泄漏
            _cleanup_engine_mcp(last_engine)
            # 用恢复的会话创建新 engine
            ctx["last_engine"] = AgentEngine(llm=llm, prompt_builder=prompt_builder,
                                              skills=skills, config=config)
            last_engine = ctx["last_engine"]
            # session.messages 只保存 role/content，无 tool_calls 结构
            # 过滤掉 tool 角色消息避免破坏 LLM API 协议（tool 消息必须紧跟在
            # 含 tool_calls 的 assistant 消息后，单独的 tool 消息会被 API 拒绝）
            last_engine.conversation = [
                {"role": m["role"], "content": m.get("content", "")}
                for m in session.messages
                if m.get("role") in ("user", "assistant")
            ]
            console.print(f"  {Symbols.INFO} 已恢复 {len(last_engine.conversation)} 条对话上下文", style="dim")
        else:
            console.print(f"  {Symbols.WARN} 未找到会话", style="yellow")
        return True

    # 未匹配返回 False
    return False
