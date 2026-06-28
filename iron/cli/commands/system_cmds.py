"""系统命令分组 — /model /skill /rules /config /agent /help /quit

从 main.py 提取的系统级命令处理逻辑，保持功能完全一致。
"""
from iron.cli.theme import Symbols
from iron.cli import ui
from iron.llm.backend import create_backend
from iron.cli.main import (
    _show_rules, _show_config, _switch_model, _safe_run_async,
)


def handle_system_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理系统命令，返回 True 表示已处理

    ctx 包含: console, config, project_root, llm, prompt_builder, skills,
              last_engine, session, loop, should_quit 等
    """
    console = ctx["console"]
    config = ctx["config"]
    llm = ctx["llm"]
    prompt_builder = ctx["prompt_builder"]
    skills = ctx["skills"]
    last_engine = ctx["last_engine"]

    if cmd == "/quit":
        # 退出提示由 main.py 的 should_quit 检查统一打印，避免重复输出
        ctx["should_quit"] = True
        return True

    elif cmd == "/help":
        ui.show_help(console)
        return True

    elif cmd == "/rules":
        _show_rules(prompt_builder, config)
        return True

    elif cmd == "/config":
        _show_config(config)
        return True

    elif cmd == "/model":
        # 保存切换前的状态快照，便于失败回滚（多厂商切换需回滚整个 provider 状态）
        _prev_active = config.active_provider
        _prev_llm_snapshot = {
            "backend": config.llm.backend,
            "model": config.llm.model,
            "api_key": config.llm.api_key,
            "base_url": config.llm.base_url,
            "available_models": list(config.llm.available_models)
                if config.llm.available_models else [],
        }
        # 备份每个 provider 的 model（_switch_model 内部可能已修改 provider.model）
        _prev_provider_models = {p.name: p.model for p in config.providers}
        new_model = _switch_model(config)
        if new_model:
            config.save()
            old_llm = llm  # 切换前保存旧引用以便 aclose
            try:
                ctx["llm"] = create_backend(config.llm.backend, config)
            except (ValueError, ImportError, AttributeError, TypeError) as e:
                console.print(f"\n  {Symbols.WARN} LLM 后端切换失败: {e}", style="yellow")
                console.print(f"  保持当前后端\n", style="dim")
                # 回滚：恢复 active_provider、llm 快照、各 provider 的 model
                config.active_provider = _prev_active
                config.llm.backend = _prev_llm_snapshot["backend"]
                config.llm.model = _prev_llm_snapshot["model"]
                config.llm.api_key = _prev_llm_snapshot["api_key"]
                config.llm.base_url = _prev_llm_snapshot["base_url"]
                config.llm.available_models = _prev_llm_snapshot["available_models"]
                for p in config.providers:
                    if p.name in _prev_provider_models:
                        p.model = _prev_provider_models[p.name]
                return True
            # aclose 旧 LLM 后端的 httpx client，避免泄漏
            # 用 _safe_run_async 兼容已有 event loop 场景，避免嵌套崩溃
            if old_llm is not None and hasattr(old_llm, "aclose"):
                _safe_run_async(old_llm.aclose(), fail_msg="清理旧 LLM 后端失败")
            # Claude Code 风格：简短一行确认，不带末尾换行（避免空行）
            console.print(f"  ⎿  已切换到 [bold]{new_model}[/bold]", style="dim")
        return True

    elif cmd == "/skill":
        skills_list = skills.list_all()
        console.print(f"\n  {Symbols.BRAIN} 可用技能\n")
        for s in skills_list:
            console.print(f"    {s.icon}  [bold]{s.name}[/bold] — {s.description}")
        console.print()
        return True

    elif cmd == "/agent":
        # /agent [name] — 上下键选择或直接切换 Agent
        # 用全局 agent_manager（ctx 注入），不依赖 last_engine，启动时即可切换
        agent_mgr = ctx.get("agent_manager")
        if agent_mgr is None:
            console.print(f"  {Symbols.WARN} Agent 管理器未初始化", style="yellow")
            return True
        # 有 args：直接切换（脚本化用法）
        if args:
            if agent_mgr.switch(args):
                agent = agent_mgr.get_current()
                console.print(f"  ⎿  已切换到 [bold]{args}[/bold] — {agent.description}", style="dim")
            else:
                console.print(f"  {Symbols.WARN} Agent '{args}' 不存在", style="yellow")
            return True
        # 无 args：上下键选择（参考 /model 的 select_with_arrows）
        agents = agent_mgr.list_agents()
        if not agents:
            console.print(f"  {Symbols.WARN} 无可用 Agent", style="yellow")
            return True
        options = []
        default_idx = 0
        for i, a in enumerate(agents):
            marker = "  ◄ 当前" if a["current"] else ""
            desc = f"{a['name']} — {a['description']}{marker}"
            if a["current"]:
                default_idx = i
            options.append((a["name"], desc))
        selected = ui.select_with_arrows(
            options, title="选择 Agent", default_idx=default_idx, console=console,
        )
        if selected is None:
            console.print(f"  {Symbols.WARN} 已取消", style="dim yellow")
            return True
        if agent_mgr.switch(selected):
            agent = agent_mgr.get_current()
            console.print(f"  ⎿  已切换到 [bold]{selected}[/bold] — {agent.description}", style="dim")
        else:
            console.print(f"  {Symbols.WARN} Agent '{selected}' 不存在", style="yellow")
        return True

    # 未匹配返回 False
    return False
