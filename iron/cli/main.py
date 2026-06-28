"""Iron CLI 主入口 — 交互式会话 + 斜杠命令"""
import asyncio
import sys
import threading
from pathlib import Path
from typing import NamedTuple, Optional, Any

import click
from rich.console import Console

from iron.cli.theme import Symbols, IRON_RULE_NAMES, set_theme
from iron.cli import ui
from iron.cli.ui import pt_prompt
from iron.config.settings import IronConfig
from iron.agent.engine import (
    AgentEngine, Phase, AgentEvent,
    TaskAgentEngine, TaskAgent,  # P1-4: TaskAgent = TaskAgentEngine 别名
    VerifyAgent,  # P3-4: 验证代理（自动跑测试 + 静态分析 + LSP 诊断）
)
from iron.agent.agent_manager import AgentManager
from iron.agent.prompt_builder import PromptBuilder
from iron.agent.conversation import ConversationSession
from iron.llm.backend import create_backend
from iron.skills.registry import SkillRegistry
from iron.rules.project_rules import create_default_rules
from iron import __version__  # 统一版本号来源

# 路径安全校验模块（由另一个工程师并行创建）；不可用时回退到内联校验
try:
    from iron.tools.path_guard import validate_path_in_project
    _HAS_PATH_GUARD = True
except ImportError:
    _HAS_PATH_GUARD = False

console = Console()

# ── 全局中断标志 ────────────────────────────────────────────────
_interrupted = threading.Event()

# 非对话命令：不注入 session（不污染对话历史给 AI 看）
# /build /flash /check 等结果需注入 session（AI 需感知），不在此集合
NON_CHAT_COMMANDS = {
    "/model", "/config", "/features", "/theme",
    "/help", "/skill", "/agent", "/rules",
    "/files", "/context", "/history",
    # v3.0: /plugin 是管理命令，不污染对话历史
    "/plugin",
    # v4.0: /git 是管理命令，不污染对话历史（用户主动查看状态/diff/log）
    "/git",
    # v4.0: /metrics 是观测性命令，不污染对话历史
    "/metrics",
}


def _clear_screen_full():
    """彻底清屏（含滚动缓冲区）

    用于 /clear 命令 —— 用户主动要求清空整个屏幕及滚动缓冲区。
    rich 的 console.clear() 只清当前屏幕，滚动条里还能看到旧内容，属于"掩耳盗铃"。

    - Windows: 用 cls（同时清当前屏幕 + 滚动缓冲区）
    - Unix: 用 \\033[2J（清屏）+ \\033[3J（清滚动缓冲区）+ \\033[H（光标归位）
    """
    import os
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()


# ── / 命令定义 ──────────────────────────────────────────────────

SLASH_COMMANDS = {
    "/code":    {"desc": "描述需求，开始编码", "handler": "handle_code"},
    "/model":   {"desc": "切换 AI 模型", "handler": "handle_model"},
    "/read":    {"desc": "读取文件内容", "handler": "handle_read"},
    "/explore": {"desc": "只读探索代码库（Task Agent）", "handler": "handle_explore"},
    "/verify": {"desc": "验证代码质量（静态分析 + LSP + 编译）", "handler": "handle_verify"},
    "/write":   {"desc": "写入文件", "handler": "handle_write"},
    "/edit":    {"desc": "编辑文件（替换内容）", "handler": "handle_edit"},
    "/delete":  {"desc": "删除文件", "handler": "handle_delete"},
    "/check":   {"desc": "运行 EmbedGuard 静态分析", "handler": "handle_check"},
    "/build":   {"desc": "编译项目", "handler": "handle_build"},
    "/flash":   {"desc": "烧录固件", "handler": "handle_flash"},
    "/monitor": {"desc": "串口监视器", "handler": "handle_monitor"},
    "/skill":   {"desc": "技能中心", "handler": "handle_skill"},
    "/rules":   {"desc": "查看/管理编码规则", "handler": "handle_rules"},
    "/config":  {"desc": "配置管理", "handler": "handle_config"},
    "/agent":   {"desc": "切换/列出 Agent", "handler": "handle_agent"},
    "/compact": {"desc": "压缩上下文", "handler": "handle_compact"},
    "/context": {"desc": "查看上下文使用情况", "handler": "handle_context"},
    "/history": {"desc": "查看历史记录", "handler": "handle_history"},
    "/resume":  {"desc": "恢复历史会话", "handler": "handle_resume"},
    "/files":   {"desc": "浏览项目文件", "handler": "handle_files"},
    "/undo":    {"desc": "撤销上次修改", "handler": "handle_undo"},
    "/clear":   {"desc": "清屏", "handler": "handle_clear"},
    "/plugin":  {"desc": "插件管理（list/search/install/remove/info）", "handler": "handle_plugin"},
    "/git":     {"desc": "Git 操作（status/diff/log/add/commit）", "handler": "handle_git"},
    "/metrics": {"desc": "查看会话指标（counters/gauges/timings）", "handler": "handle_metrics"},
    "/help":    {"desc": "显示帮助", "handler": "handle_help"},
    "/quit":    {"desc": "退出", "handler": "handle_quit"},
}

# 选项文本常量（避免硬编码中文导致拼写不一致）
_OPTION_COMPILE = "编译试试"
_OPTION_QUIT = "退出"


def _validate_project_path(file_path: str, project_root, allow_create: bool = False) -> Path:
    """校验文件路径位于项目目录内，返回解析后的绝对路径。

    优先使用 path_guard 模块（如已安装）；否则回退到内联校验。
    越界时抛出 ValueError。
    """
    if _HAS_PATH_GUARD:
        return validate_path_in_project(file_path, str(project_root), allow_create=allow_create)
    # 内联校验回退方案
    project_root_resolved = Path(str(project_root)).resolve()
    full_path = (project_root_resolved / file_path).resolve()
    try:
        full_path.relative_to(project_root_resolved)
    except ValueError:
        raise ValueError("路径越界：禁止访问项目目录外的文件")
    if not allow_create and not full_path.exists():
        raise ValueError(f"文件不存在: {file_path}")
    return full_path


def _safe_run_async(coro, fail_msg: str = "异步操作失败"):
    """安全运行 async 协程，兼容已有 event loop 场景

    当前 run_interactive 是同步函数，asyncio.run 暂时安全。
    若未来主循环改为 async，本函数避免嵌套崩溃：
    - 已在 event loop 中：用 ensure_future 调度（无法等待结果）
    - 不在 event loop 中：用 asyncio.run
    """
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在 event loop 中，用 ensure_future 调度（无法等待结果）
                asyncio.ensure_future(coro)
                return None
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            # 没有 event loop，用 asyncio.run
            return asyncio.run(coro)
    except (ValueError, RuntimeError, TypeError) as e:
        console.print(f"  {fail_msg}: {e}", style="dim yellow")
        return None


def _cleanup_engine_mcp(last_engine):
    """清理旧 engine 的 MCP 客户端，避免子进程泄漏

    每次 _run_agent 创建新 AgentEngine 时，旧 engine 的 MCP 子进程需要显式 disconnect，
    否则前面 engine 的 MCP 子进程会成为孤儿（只有最后一个 last_engine 在退出时被清理）。
    """
    if last_engine is not None and getattr(last_engine, "_mcp_client", None):
        _safe_run_async(
            last_engine._mcp_client.disconnect_all(),
            fail_msg="清理旧 MCP 客户端失败",
        )


def _cleanup_lsp(lsp_client):
    """清理 LSP 客户端子进程，避免 clangd/ccls 孤儿进程

    LSP 客户端在 bootstrap 阶段 3 创建，需在 run_interactive 退出时显式 stop()，
    否则 clangd/ccls 子进程会成为孤儿（仅靠 GC 无法可靠终止）。

    约束 C1：清理失败不阻塞退出，仅打印警告。
    """
    if lsp_client is not None:
        _safe_run_async(
            lsp_client.stop(),
            fail_msg="清理 LSP 客户端失败",
        )


def _inject_cli_event_to_session(session, event_type: str, summary: str, details: dict):
    """把 CLI 层事件注入 session conversation

    用户反馈：执行 /build 等命令后，AI 不知道发生了什么（"除了他自己输出的内容，
    其他内容他根本识别不到"）。原因是 CLI 层事件（编译结果、选项选择等）只显示到控制台，
    没有加入 conversation，AI 自然无法识别。

    本函数把 CLI 事件包装成 user 消息（带 [CLI事件] 前缀），注入到 session，
    让 AI 在后续对话中能看到这些事件。
    """
    try:
        parts = [f"[CLI事件/{event_type}] {summary}"]
        for k, v in details.items():
            if v:
                parts.append(f"  {k}: {v}")
        msg = "\n".join(parts)
        session.add_message("user", msg)
    except (ValueError, TypeError):
        pass  # 注入失败不应影响 CLI 主流程


def _count_output_tokens(text: str) -> int:
    """计算输出文本的 token 数

    优先用 tiktoken 精确计数，不可用时 fallback 到字符数 / 4 估算。
    流式输出的每个 chunk 调用一次，累加得到总输出 token 数。
    """
    if not text:
        return 0
    try:
        from iron.utils.token_counter import count_tokens
        return count_tokens(text)
    except ImportError:
        return max(1, len(text) // 4)


# ── Click CLI ───────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.option("--mcu", default=None, help="目标 MCU (如 stm32f407)")
@click.option("--model", default=None, help="AI 模型 (如 gpt-4o)")
@click.option("--backend", default=None, help="LLM 后端 (openai/anthropic/ollama/echo)")
@click.option("--project", default=".", help="项目目录路径")
@click.option("--remote", default=None,
              help="远程模式：[user@]host[:port]:/path（实验性）")
@click.option("--verbose", is_flag=True, help="详细输出模式")
@click.version_option(version=__version__, prog_name="Iron")  # 从 __init__.py 读取版本号
@click.pass_context
def cli(ctx, mcu, model, backend, project, remote, verbose):
    """Iron — 嵌入式 AI 开发 Agent CLI"""
    ctx.ensure_object(dict)

    # P6-1: 启动管道分阶段（配置/信任/运行）
    project_root = Path(project).resolve()
    if not project_root.exists():
        console.print(f"  ⚠ 项目路径不存在: {project_root}", style="red")
        sys.exit(1)

    # v3.0: 远程模式（实验性）
    if remote:
        try:
            from iron.remote import parse_remote_spec, create_executor
            remote_spec = parse_remote_spec(remote)
            ctx.obj["remote_executor"] = create_executor(remote)
            ctx.obj["remote_spec"] = remote_spec
            console.print(f"  ⚡ 远程模式: {remote_spec.target()}:{remote_spec.path}",
                          style="cyan")
        except (ValueError, ImportError, RuntimeError) as e:
            console.print(f"  ⚠ 远程模式初始化失败，回退到本地: {e}", style="yellow")
            ctx.obj["remote_executor"] = None
            ctx.obj["remote_spec"] = None
    else:
        ctx.obj["remote_executor"] = None
        ctx.obj["remote_spec"] = None

    # 用 Bootstrap 替代直接加载 — 分阶段初始化 + 进度显示 + 错误隔离
    from iron.cli.bootstrap import Bootstrap
    bootstrap = Bootstrap(console)
    result = bootstrap.run(project_root, mcu, model, backend, verbose)

    if not result.success:
        for err in result.errors:
            console.print(f"  ✗ {err}", style="red")
        sys.exit(1)

    for warn in result.warnings:
        console.print(f"  ⚠ {warn}", style="yellow")

    config = result.config
    ctx.obj["config"] = config
    ctx.obj["project_root"] = project_root
    ctx.obj["llm"] = result.llm
    ctx.obj["prompt_builder"] = result.prompt_builder
    ctx.obj["skills"] = result.skills
    ctx.obj["lsp_client"] = result.lsp_client

    # 如果没有子命令，进入交互模式
    if ctx.invoked_subcommand is None:
        run_interactive(config, project_root, lsp_client=result.lsp_client)


@cli.command()
@click.argument("prompt")
@click.option("--mcu", default=None, help="目标 MCU")
@click.option("--output", "-o", default=".", help="输出目录")
@click.pass_context
def run(ctx, prompt, mcu, output):
    """单次编码模式（非交互）"""
    config = ctx.obj["config"]
    if mcu:
        config.project.mcu = mcu
    project_root = Path(output).resolve()
    asyncio.run(run_single(config, project_root, prompt))


@cli.command()
@click.option("--mcu", default="stm32f407", help="目标 MCU")
@click.pass_context
def init(ctx, mcu):
    """初始化 .iron-agent/ 项目配置"""
    project_root = ctx.obj.get("project_root", Path.cwd())
    rules_dir = create_default_rules(project_root, mcu)
    console.print(f"\n{Symbols.CHECK} 已初始化 .iron-agent/ 目录")
    console.print(f"  {Symbols.FOLDER} {rules_dir.parent}")
    console.print(f"  {Symbols.FILE_NEW} {rules_dir / 'target-mcu.md'}")
    console.print(f"  {Symbols.FILE_NEW} {rules_dir / 'coding-standards.md'}")
    console.print(f"  {Symbols.FILE_NEW} {rules_dir.parent / 'instructions.md'}")
    console.print(f"\n  编辑这些文件来自定义编码规则和项目配置。\n")


@cli.command(name="check")
@click.argument("paths", nargs=-1)
@click.option("--mcu", default=None, help="目标 MCU")
@click.pass_context
def check_cmd(ctx, paths, mcu):
    """运行 EmbedGuard 静态分析"""
    config = ctx.obj["config"]
    if mcu:
        config.project.mcu = mcu
    if not paths:
        paths = ("src/",)
    console.print(f"\n{Symbols.SHIELD} EmbedGuard 静态分析")
    console.print(f"  目标 MCU: {config.project.mcu.upper()}")
    console.print(f"  扫描路径: {', '.join(paths)}\n")

    # 尝试调用 EmbedGuard
    try:
        from iron.integrations.embedguard_bridge import analyze_paths
        findings = analyze_paths(paths, config.project.mcu)
        if findings:
            ui.show_findings(console, findings)
        else:
            console.print(f"  {Symbols.CHECK} 未发现问题，代码通过静态分析！\n", style="green")
    except ImportError as e:
        # 给出具体的 pip install 安装指引
        missing = str(e)
        console.print(f"  {Symbols.WARN} EmbedGuard 未安装或依赖缺失\n", style="yellow")
        console.print(f"    缺失: {missing}\n")
        console.print(f"  {Symbols.INFO} 安装指引:\n", style="cyan")
        if "tree_sitter" in missing or "tree-sitter" in missing:
            console.print(f"    pip install tree_sitter tree_sitter_c tree_sitter_python\n")
            console.print(f"    {Symbols.INFO} 安装后 EmbedGuard 的 AST 解析才能正常工作\n", style="cyan")
        elif "embedguard" in missing:
            console.print(f"    pip install embedguard\n")
            console.print(f"    {Symbols.INFO} 或从源码安装: cd 嵌入式-embedguard && pip install -e .\n", style="cyan")
        else:
            console.print(f"    pip install embedguard tree_sitter tree_sitter_c\n")
    except (ImportError, AttributeError, TypeError) as e:
        ui.show_error(console, f"分析失败: {e}")


@cli.command()
def config():
    """配置模型（交互式设置 URL + API Key）"""
    IronConfig.setup_interactive()


@cli.command()
@click.pass_context
def skill(ctx):
    """查看所有可用技能"""
    registry = SkillRegistry()
    skills = registry.list_all()
    console.print(f"\n  {Symbols.BRAIN} 可用技能 ({len(skills)} 个)\n")
    for s in skills:
        console.print(f"    {s.icon}  [bold]{s.name}[/bold] — {s.description}")
    console.print()


@cli.command()
@click.pass_context
def doctor(ctx):
    """检查环境依赖"""
    console.print(f"\n  {Symbols.WRENCH} Iron 环境检查\n")
    checks = [
        ("Python", sys.version.split()[0], True),
        ("Rich", _check_import("rich"), True),
        ("Prompt Toolkit", _check_import("prompt_toolkit"), True),
        ("Click", _check_import("click"), True),
        ("HTTPX", _check_import("httpx"), True),
        ("PyYAML", _check_import("yaml"), True),
        ("PySerial", _check_import("serial"), False),
        ("Tree-sitter", _check_import("tree_sitter"), False),
        ("EmbedForge", _check_deep_import("embedforge", ["servers.build_server.server"]), False),
        ("EmbedGuard", _check_deep_import("embedguard", ["core.pipeline", "core.ast_parser"]), False),
    ]
    for name, version, required in checks:
        if version:
            icon = Symbols.CHECK
            style = "green"
        elif required:
            icon = Symbols.CROSS
            style = "red"
        else:
            icon = Symbols.WARN
            style = "yellow"
        console.print(f"    {icon} {name}: {version or '未安装'}", style=style)
    console.print()

    # tree-sitter 详细检测（含 tree_sitter_c 语言包）
    _ts_version = _check_import("tree_sitter")
    _ts_c_version = _check_import("tree_sitter_c")
    if _ts_version and _ts_c_version:
        console.print(f"    {Symbols.CHECK} tree_sitter_c: {_ts_c_version}", style="green")
    elif _ts_version and not _ts_c_version:
        console.print(f"    {Symbols.WARN} tree_sitter_c: 未安装（仅装了 tree_sitter）", style="yellow")
        console.print(f"      补装: [cyan]python -m pip install tree_sitter_c[/cyan]")
        console.print(f"      一键启用: [cyan]iron code-indexer init[/cyan]")
    else:
        console.print(f"    {Symbols.WARN} tree-sitter: 未安装", style="yellow")
        console.print(f"      安装: [cyan]python -m pip install tree_sitter tree_sitter_c[/cyan]")
        console.print(f"      一键启用: [cyan]iron code-indexer init[/cyan]")
    # 特性门控状态
    try:
        from iron.config.features import is_feature_enabled
        _ci_enabled = is_feature_enabled("code_indexer")
        _ci_status = "已启用" if _ci_enabled else "未启用"
        console.print(f"    {Symbols.INFO} code_indexer 特性: {_ci_status}", style="dim")
    except ImportError:
        pass
    console.print()


@cli.group(name="code-indexer")
def code_indexer_grp():
    """代码索引管理（tree-sitter 安装 + 特性启用）"""
    pass


@code_indexer_grp.command()
def init():
    """初始化代码索引（安装依赖 + 启用特性）"""
    import subprocess
    console.print(f"\n  {Symbols.WRENCH} 代码索引初始化\n")

    # 步骤 1：检测/安装依赖
    _ts_ok = _check_import("tree_sitter") and _check_import("tree_sitter_c")
    if _ts_ok:
        console.print(f"  {Symbols.CHECK} tree-sitter 已安装")
    else:
        console.print(f"  {Symbols.INFO} 安装 tree-sitter...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install",
                 "tree_sitter", "tree_sitter_c"],
                check=True,
            )
            console.print(f"  {Symbols.CHECK} tree-sitter 安装成功")
        except subprocess.CalledProcessError as e:
            console.print(f"  {Symbols.CROSS} 安装失败: {e}", style="red")
            console.print(f"  手动安装: [cyan]python -m pip install tree_sitter tree_sitter_c[/cyan]")
            sys.exit(1)
        except (OSError, FileNotFoundError) as e:
            console.print(f"  {Symbols.CROSS} 无法启动 pip: {e}", style="red")
            sys.exit(1)

    # 步骤 2：启用特性
    try:
        from iron.config.features import get_feature_flags
        flags = get_feature_flags()
        if not flags.enable("code_indexer"):
            console.print(f"  {Symbols.WARN} 特性 code_indexer 不存在于注册表", style="yellow")
        else:
            flags.save()
            console.print(f"  {Symbols.CHECK} 特性 code_indexer=True 已启用")
    except (OSError, ValueError, ImportError) as e:
        console.print(f"  {Symbols.WARN} 启用特性失败: {e}", style="yellow")
        console.print(f"    手动编辑 ~/.iron/features.yml: code_indexer: true")

    console.print(f"\n  {Symbols.DONE} 代码索引已就绪，下次启动 iron 时生效\n")


@code_indexer_grp.command()
def status():
    """查看代码索引状态"""
    console.print(f"\n  {Symbols.WRENCH} 代码索引状态\n")

    # tree-sitter 安装状态
    _ts_version = _check_import("tree_sitter")
    _ts_c_version = _check_import("tree_sitter_c")
    if _ts_version and _ts_c_version:
        console.print(f"  {Symbols.CHECK} tree-sitter: 已安装 ({_ts_version})")
        console.print(f"  {Symbols.CHECK} tree_sitter_c: 已安装 ({_ts_c_version})")
    elif _ts_version:
        console.print(f"  {Symbols.WARN} tree-sitter: 已安装但缺 tree_sitter_c", style="yellow")
        console.print(f"    补装: [cyan]python -m pip install tree_sitter_c[/cyan]")
    else:
        console.print(f"  {Symbols.CROSS} tree-sitter: 未安装", style="red")
        console.print(f"    安装: [cyan]iron code-indexer init[/cyan]")

    # 特性门控状态
    try:
        from iron.config.features import is_feature_enabled
        enabled = is_feature_enabled("code_indexer")
        status_str = "已启用" if enabled else "未启用"
        icon = Symbols.CHECK if enabled else Symbols.WARN
        console.print(f"  {icon} 特性 code_indexer: {status_str}")
    except ImportError:
        console.print(f"  {Symbols.WARN} 特性门控模块不可用", style="yellow")

    # CodeIndexer 实例状态（仅当 tree-sitter 可用时）
    if _ts_version and _ts_c_version:
        try:
            from iron.integrations.code_indexer import CodeIndexer
            # 仅检测可用性，不真创建 db
            ci_available = CodeIndexer.__init__.__doc__ or ""
            console.print(f"  {Symbols.INFO} CodeIndexer 类已加载")
        except (ImportError, AttributeError) as e:
            console.print(f"  {Symbols.WARN} CodeIndexer 加载失败: {e}", style="yellow")

    console.print()


def _check_import(module_name: str) -> str:
    """浅层导入检查（仅检查顶层模块）"""
    try:
        mod = __import__(module_name)
        return getattr(mod, "__version__", "已安装")
    except ImportError:
        return ""


def _check_deep_import(module_name: str, submodules: list[str] | None = None) -> str:
    """深层导入检查（检查核心子模块能否导入）

    与 check 命令的导入路径保持一致，避免 doctor 报告已安装但 check 报错。
    """
    try:
        mod = __import__(module_name)
        # 检查核心子模块
        if submodules:
            for sub in submodules:
                try:
                    __import__(f"{module_name}.{sub}")
                except ImportError as e:
                    return f"部分可用（{sub} 导入失败: {e}）"
        return getattr(mod, "__version__", "已安装")
    except ImportError:
        return ""


# ── 交互式会话 ──────────────────────────────────────────────────


class ComponentBundle(NamedTuple):
    """run_interactive 初始化阶段产出的组件集合。

    所有字段在初始化后不再变更（last_engine / last_options 等可变状态
    不放入 bundle，由主循环局部变量承载）。
    """
    prompt_builder: PromptBuilder
    skills: SkillRegistry
    llm: Any                      # Backend 实例（可能为 EchoBackend）
    db: Optional[Any]             # Database 实例，降级时为 None
    session: ConversationSession
    completer: Any                # ui.CommandCompleter 实例
    total_rules: int
    code_indexer: Optional[Any] = None   # v3.0: CodeIndexer（特性门控 + db 可用时创建）
    plugin_manager: Optional[Any] = None  # v3.0: PluginManager（特性门控开启时创建）


class SlashResult(NamedTuple):
    """_dispatch_slash_command 的返回值。"""
    should_quit: bool
    last_engine: Optional[AgentEngine]
    last_options: list


def _init_session_components(config: IronConfig, project_root: Path) -> ComponentBundle:
    """初始化会话组件 — 创建 PromptBuilder/Skills/LLM/SQLite/session/completer

    返回 ComponentBundle（不可变），last_engine/last_options 等可变状态
    由主循环局部变量承载，不放入 bundle。
    """
    prompt_builder = PromptBuilder(project_root, config.project.mcu)
    rules_count = prompt_builder.count_active_rules()
    total_rules = sum(rules_count)

    # 加载用户自定义 skill（从 .iron/skills/ 目录）
    skills = SkillRegistry()
    user_skills_dir = project_root / ".iron" / "skills"
    if user_skills_dir.exists():
        skills.load_from_dir(user_skills_dir)

    # 创建 LLM 后端
    try:
        llm = create_backend(config.llm.backend, config)
    except (ValueError, ImportError, TypeError) as e:
        console.print(f"\n  {Symbols.WARN} LLM 后端初始化失败: {e}", style="yellow")
        console.print(f"  使用 Echo 模式（仅返回占位代码）\n", style="dim")
        from iron.llm.backend import EchoBackend
        llm = EchoBackend()

    # P3-2: 初始化 SQLite 持久化层（失败时降级到 JSON-only 模式）
    db = None
    try:
        from iron.core.db import Database
        db = Database()
        db.connect()
    except Exception as e:
        console.print(f"  {Symbols.WARN} SQLite 持久化初始化失败，使用 JSON-only 模式: {e}", style="dim yellow")
        db = None

    # 创建会话
    session = ConversationSession(mcu=config.project.mcu, project_dir=str(project_root))
    completer = ui.CommandCompleter()

    # v3.0: 代码索引器（特性门控 + db 可用 + tree-sitter 可用时创建）
    code_indexer = None
    if db is not None:
        try:
            from iron.config.features import is_feature_enabled
            if is_feature_enabled("code_indexer"):
                from iron.integrations.code_indexer import CodeIndexer
                code_indexer = CodeIndexer(db, str(project_root))
                if code_indexer._has_ts:  # tree-sitter 真正可用
                    console.print(f"  {Symbols.CHECK} 代码索引已启用（tree-sitter）",
                                  style="green")
                else:
                    console.print(f"  {Symbols.WARN} 代码索引降级模式（tree-sitter 未安装）",
                                  style="yellow")
                    code_indexer = None  # 降级时置 None，避免半残状态
        except Exception as e:
            console.print(f"  {Symbols.WARN} 代码索引初始化失败: {e}", style="dim yellow")
            code_indexer = None

    # v3.0: 插件管理器（特性门控开启时创建）
    plugin_manager = None
    try:
        from iron.config.features import is_feature_enabled
        if is_feature_enabled("plugins"):
            from iron.plugins import PluginManager, PluginContext
            plugins_dir = project_root / ".iron-agent" / "plugins"
            # 构造受控 context：默认授予 file_read + file_write + run_command
            # 用户可通过 plugin.json 的 permissions 字段进一步约束
            ctx = PluginContext(
                project_root=str(project_root),
                config=config,
                permissions=["file_read", "file_write", "run_command"],
            )
            plugin_manager = PluginManager(plugins_dir=str(plugins_dir), context=ctx)
            plugin_manager.discover()
            console.print(f"  {Symbols.CHECK} 插件系统已启用", style="green")
    except Exception as e:
        console.print(f"  {Symbols.WARN} 插件系统初始化失败: {e}", style="dim yellow")
        plugin_manager = None

    return ComponentBundle(
        prompt_builder=prompt_builder,
        skills=skills,
        llm=llm,
        db=db,
        session=session,
        completer=completer,
        total_rules=total_rules,
        code_indexer=code_indexer,
        plugin_manager=plugin_manager,
    )


def _show_startup_info(config: IronConfig, llm, total_rules: int, project_root: Path) -> None:
    """显示启动信息 — 欢迎界面 + Echo 警告 / API Key 显示

    纯显示函数，无状态变更。echo 分支只打印警告，非 echo 分支显示 API Key。
    """
    ui.show_welcome(
        console,
        version=__version__,
        mcu=config.project.mcu.upper(),
        model=f"{config.llm.backend}/{config.llm.model}",
        project_dir=str(project_root),
        rules_count=total_rules,
        build_system=config.project.build_system,
    )

    if config.llm.backend == "echo":
        console.print(f"  {Symbols.WARN} [yellow]当前使用 Echo 模式，AI 响应为占位代码，仅用于测试。"
                      f"生产环境请在 iron.yml 中配置真实 LLM 后端（openai/anthropic/ollama）。[/yellow]")

    # 显示 API key 前 4 位和后 4 位，帮助用户确认 key 是否生效
    _key = config.llm.api_key
    if _key and len(_key) > 12:
        _key_display = f"{_key[:4]}...{_key[-4:]}"
    elif _key:
        _key_display = f"{_key[:4]}***"
    else:
        _key_display = "[red]未设置[/red]"
    # 多厂商场景：显示当前 active provider 名称和厂商总数
    _active_name = config.active_provider or (config.providers[0].name if config.providers else "")
    _provider_count = len(config.providers)
    if _provider_count > 1 and _active_name:
        console.print(f"  {Symbols.INFO} 厂商: [cyan]{_active_name}[/cyan] "
                      f"[dim](共 {_provider_count} 个，用 /model 切换)[/dim]", style="dim")
    console.print(f"  {Symbols.INFO} 后端: [cyan]{config.llm.backend}[/cyan]  "
                  f"模型: [cyan]{config.llm.model}[/cyan]  "
                  f"API Key: [dim]{_key_display}[/dim]", style="dim")
    # API Key 未设置时给出具体设置方法
    if not _key:
        # 优先用 active provider 对应的环境变量名
        _active_provider = config.get_active_provider() if config.providers else None
        _env_var = _active_provider.env_var_name if _active_provider else "IRON_API_KEY"
        console.print(f"  [yellow]⚠ API Key 未设置，请用以下任一方式配置后重启终端：[/yellow]")
        console.print(f"  [dim]1. PowerShell 永久：[/dim] [cyan][Environment]::SetEnvironmentVariable('{_env_var}','你的key','User')[/cyan]")
        console.print(f"  [dim]2. CMD 临时：[/dim]      [cyan]set {_env_var}=你的key[/cyan]")
        if _env_var != "IRON_API_KEY":
            console.print(f"  [dim]3. 第一个厂商也兼容：[/dim] [cyan]IRON_API_KEY[/cyan] [dim]或[/dim] [cyan]OPENAI_API_KEY[/cyan]")


def _handle_numeric_input(text: str, last_options: list) -> tuple[str | None, bool]:
    """数字选择 → 映射为选项文本。

    返回 (mapped_text, should_quit)：
    - 非数字 / 越界：(None, False)，调用方按原 text 处理
    - _OPTION_COMPILE → ("/build", False)
    - _OPTION_QUIT    → (None, True)，调用方应 break
    - 其他            → (selected_option, False)
    """
    if not (text.isdigit() and last_options):
        return None, False
    idx = int(text) - 1
    if not (0 <= idx < len(last_options)):
        return None, False
    selected_option = last_options[idx]
    console.print(f"  → {selected_option}", style="dim cyan")
    if selected_option == _OPTION_COMPILE:
        return "/build", False
    if selected_option == _OPTION_QUIT:
        return None, True
    return selected_option, False


def _dispatch_slash_command(text: str, cmd_ctx: dict, last_options: list) -> SlashResult:
    """斜杠命令 4 路分发 + 状态双向同步

    调用前需把 last_engine 写入 cmd_ctx["last_engine"]；
    调用后从返回值取 last_engine，从 cmd_ctx 取 session/llm/config。
    """
    # 延迟导入命令分组（避免循环依赖）
    from iron.cli.commands import (
        handle_file_commands, handle_build_commands,
        handle_session_commands, handle_system_commands,
    )
    from iron.cli.commands.plugin_cmds import handle_plugin_commands
    from iron.cli.commands.git_cmds import handle_git_commands
    from iron.cli.commands.metrics_cmds import handle_metrics_commands
    cmd = text.split()[0].lower()
    args = text[len(cmd):].strip()

    # 非对话命令：不注入 session（避免污染对话历史给 AI 看）
    _is_non_chat = cmd in NON_CHAT_COMMANDS
    if not _is_non_chat:
        cmd_ctx["session"].add_message("user", text)

    if handle_file_commands(cmd, args, cmd_ctx):
        pass
    elif handle_build_commands(cmd, args, cmd_ctx):
        pass
    elif handle_session_commands(cmd, args, cmd_ctx):
        pass
    elif handle_system_commands(cmd, args, cmd_ctx):
        pass
    elif handle_plugin_commands(cmd, args, cmd_ctx):
        pass
    elif handle_git_commands(cmd, args, cmd_ctx):
        pass
    elif handle_metrics_commands(cmd, args, cmd_ctx):
        pass
    else:
        console.print(f"  未知命令: {cmd}，输入 /help 查看可用命令", style="yellow")

    should_quit = cmd_ctx["should_quit"]
    if should_quit:
        console.print(f"\n  再见! {Symbols.DONE}\n")
    return SlashResult(
        should_quit=should_quit,
        last_engine=cmd_ctx["last_engine"],
        last_options=last_options,
    )


def _install_sigterm_handler() -> dict:
    """注册 SIGTERM 信号处理（Unix only；Windows 自动跳过）

    返回 {"flag": False} dict，主循环检查 flag 后 break，确保 session.save 被执行。
    """
    _sigterm_received = {"flag": False}
    import signal as _signal
    def _sigterm_handler(signum, frame):
        _sigterm_received["flag"] = True
    if hasattr(_signal, "SIGTERM"):
        try:
            _signal.signal(_signal.SIGTERM, _sigterm_handler)
        except (ValueError, OSError):
            pass  # 非主线程无法注册信号
    return _sigterm_received


def _cleanup_on_exit(session, db, last_engine, llm, loop, lsp_client=None) -> None:
    """主循环退出后的资源清理

    顺序敏感：session.save → db.close → MCP disconnect → llm.aclose → LSP stop
    → shutdown_asyncgens → cancel pending → loop.close → set_event_loop(None)
    所有步骤均 try/except 包裹，单步失败不阻断后续清理。
    """
    # 保存会话
    try:
        sessions_dir = Path.home() / ".iron" / "sessions"
        session.save(sessions_dir, db=db)
    except OSError as e:
        console.print(f"  ⚠ 会话保存失败: {e}", style="dim yellow")
    finally:
        # P3-2: 关闭 SQLite 连接
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    # 主循环退出后清理 MCP 子进程与 LLM httpx client，避免资源泄漏
    # 用 _safe_run_async 兼容已有 event loop 场景，避免嵌套崩溃
    try:
        if last_engine is not None and getattr(last_engine, "_mcp_client", None):
            loop.run_until_complete(last_engine._mcp_client.disconnect_all())
        if llm is not None and hasattr(llm, "aclose"):
            loop.run_until_complete(llm.aclose())
        # LSP 客户端清理（约束 C1：失败不阻塞退出）
        if lsp_client is not None:
            try:
                loop.run_until_complete(lsp_client.stop())
            except (RuntimeError, OSError) as e:
                console.print(f"  LSP 清理失败: {e}", style="dim yellow")
    except (RuntimeError, OSError) as e:
        console.print(f"  资源清理失败: {e}", style="dim yellow")
    finally:
        # 关闭所有未完成的 async generator（如 stream_generate），
        # 避免 "Task was destroyed but it is pending! ... async_generator_athrow"
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except (RuntimeError, Exception):
            pass
        # 取消所有挂起任务，防止 loop.close() 时触发 "Task was destroyed"
        try:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except (RuntimeError, Exception):
            pass
        # 关闭会话事件循环，避免资源泄漏
        try:
            loop.close()
        except RuntimeError:
            pass
        asyncio.set_event_loop(None)


def run_interactive(config: IronConfig, project_root: Path, lsp_client=None):
    """启动交互式会话"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _sigterm_received = _install_sigterm_handler()

    bundle = _init_session_components(config, project_root)
    _show_startup_info(config, bundle.llm, bundle.total_rules, project_root)
    console.print(f"  输入需求开始编码，或输入 [bold cyan]/help[/bold cyan] 查看命令\n")

    last_engine: AgentEngine | None = None
    last_options: list[str] = []

    # 独立 AgentManager — 启动时即创建，让用户在任何时候（含对话前）都能切换 agent
    # 创建 engine 后会把此实例注入到 engine._agent_manager，保证状态共享
    agent_manager = AgentManager(str(project_root))

    cmd_ctx = {
        "console": console, "config": config, "project_root": project_root,
        "llm": bundle.llm, "prompt_builder": bundle.prompt_builder,
        "skills": bundle.skills, "last_engine": last_engine,
        "session": bundle.session, "loop": loop,
        "total_rules": bundle.total_rules, "should_quit": False,
        "lsp_client": lsp_client,
        "agent_manager": agent_manager,
        # v3.0: 注入 CodeIndexer / PluginManager 供 /plugin 命令与语义工具使用
        "code_indexer": bundle.code_indexer,
        "plugin_manager": bundle.plugin_manager,
    }

    while True:
        if _sigterm_received["flag"]:
            console.print(f"\n  {Symbols.WARN} 收到退出信号，正在保存会话...", style="yellow")
            break
        # 当前 agent 名（显示在提示符中，便于用户感知当前运行的 agent）
        # 用全局 agent_manager，无需依赖 last_engine — 启动时即可切换
        _current_agent = agent_manager.get_current_name()
        try:
            user_input = ui.get_user_input(console, bundle.completer, current_agent=_current_agent)
        except KeyboardInterrupt:
            if _sigterm_received["flag"]:
                break
            _sigterm_received["flag"] = True
            console.print(f"\n  {Symbols.WARN} 再按一次 Ctrl+C 退出（会话将保存），或继续输入", style="dim yellow")
            continue
        except EOFError:
            console.print(f"\n  再见! {Symbols.DONE}\n")
            break

        if user_input == "__UNDO__":
            if last_engine is not None:
                _do_undo(console, last_engine)
            else:
                console.print(f"  {Symbols.WARN} 没有可撤销的操作", style="dim yellow")
            continue

        if user_input == "__SWITCH_AGENT__":
            # Tab 键触发 agent 切换（等价于 /agent 命令，启动时即可使用）
            cmd_ctx["last_engine"] = last_engine
            result = _dispatch_slash_command("/agent", cmd_ctx, last_options)
            last_engine = result.last_engine
            continue

        if not user_input.strip():
            continue
        text = user_input.strip()

        mapped, quit_flag = _handle_numeric_input(text, last_options)
        if quit_flag:
            console.print(f"\n  再见! {Symbols.DONE}\n")
            break
        if mapped is not None:
            text = mapped

        if text.startswith("/"):
            cmd_ctx["last_engine"] = last_engine
            cmd_ctx["config"] = config
            result = _dispatch_slash_command(text, cmd_ctx, last_options)
            last_engine = result.last_engine
            config = cmd_ctx["config"]
            if result.should_quit:
                break
            continue

        cmd_ctx["session"].add_message("user", text)
        console.print()
        _prior_conv = last_engine.conversation if last_engine else None
        _cleanup_engine_mcp(last_engine)
        last_engine = loop.run_until_complete(_run_agent(
            console, cmd_ctx["llm"], bundle.prompt_builder, bundle.skills,
            config, cmd_ctx["session"], text, last_options,
            prior_conversation=_prior_conv,
            lsp_client=cmd_ctx["lsp_client"],
            code_indexer=cmd_ctx["code_indexer"],
            agent_manager=agent_manager,
        ))

    _cleanup_on_exit(cmd_ctx["session"], bundle.db, last_engine, cmd_ctx["llm"], loop, lsp_client)


class _ThinkingSpinner:
    """持久化思考动画 — 用 rich.status 实现，持续到 AI 回复

    OpenCode 风格：左侧 spinner + 状态文字，持续旋转直到完成。
    记录思考开始时间，stop 时显示耗时。
    思考中实时显示用时 + 输出 token 数（Claude Code 风格）
    """

    def __init__(self, console: Console):
        self._console = console
        self._status = None
        self._message = "思考中..."
        self._spinner_name = "dots"
        self._start_time = 0.0
        self._elapsed = 0.0
        # 实时显示相关
        self._input_tokens = 0
        self._output_tokens = 0
        self._timer_thread = None
        self._timer_stop = threading.Event()
        self._streamed = False  # 是否已开始流式输出（用于区分"思考中"和"输出中"状态）
        self._lock = threading.Lock()  # 保护共享状态，防止 timer 与 stop 竞态
        # 首次启动时间（整个请求的总计时基准，避免多次 start 重置）
        self._first_start_time = 0.0

    def start(self, message: str = "思考中...", input_tokens: int = 0):
        """启动 spinner + 实时计时线程

        策略：第一次渲染不带 input_tokens，让用户看到数字"跳"进去（有动态感）。
        修复：多次 start 调用不再重置计时，_first_start_time 只在首次设置。
        """
        import time as _time
        self._message = message
        # 仅首次启动时记录起始时间 + 设置 input_tokens（避免多步 thinking 重置计时）
        if self._first_start_time == 0.0:
            self._first_start_time = _time.time()
            self._input_tokens = input_tokens
        # _start_time 始终基于首次启动（计时连续，不重置）
        self._start_time = self._first_start_time
        # 不清零 _output_tokens（累加保留，反映整个请求累计 token）
        # _streamed 只在 stop 或新请求时重置，不在 start 时重置
        # 先不带 token 数字渲染，让它动起来
        if self._status is None:
            self._status = self._console.status(
                self._render_status(message, 0.0, 0, 0),
                spinner=self._spinner_name,
                spinner_style="cyan",
            )
            self._status.__enter__()
        else:
            self.update(message)
        # 启动实时计时更新线程（会在 0.25s 后补上 token 数字）
        if self._timer_thread is None or not self._timer_thread.is_alive():
            self._timer_stop.clear()
            self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
            self._timer_thread.start()

    def _timer_loop(self):
        """后台计时线程，每 0.25s 更新一次状态显示

        使用 self._lock 保护共享状态，避免与 stop()/update() 竞态。
        收到停止信号或 console 被关闭时优雅退出。
        """
        import time as _time
        while not self._timer_stop.is_set():
            with self._lock:
                if self._status and self._start_time > 0:
                    elapsed = _time.time() - self._start_time
                    try:
                        self._status.update(
                            self._render_status(
                                self._message, elapsed,
                                self._input_tokens, self._output_tokens,
                            )
                        )
                    except RuntimeError:
                        pass  # console 已关闭，优雅退出
            _time.sleep(0.25)

    @staticmethod
    def _render_status(message: str, elapsed: float, in_tokens: int, out_tokens: int) -> str:
        """渲染状态文字：message (X.Ys)

        精简模式（模仿 Claude Code）：只显示 message + 计时，不显示实时 token。
        token 统计在 chat_response 完成时一次性显示。
        """
        parts = [f"[dim cyan]{message}[/dim cyan]"]
        if elapsed > 0:
            if elapsed < 1.0:
                pass  # 第一秒不显示计时，避免闪烁
            elif elapsed < 10.0:
                parts.append(f"[dim]({elapsed:.1f}s)[/dim]")
            else:
                parts.append(f"[dim]({elapsed:.0f}s)[/dim]")
        return " ".join(parts)

    def update(self, message: str):
        """更新提示文字"""
        self._message = message
        if self._status:
            import time as _time
            elapsed = _time.time() - self._start_time if self._start_time > 0 else 0.0
            self._status.update(
                self._render_status(
                    message, elapsed,
                    self._input_tokens, self._output_tokens,
                )
            )

    def set_input_tokens(self, token_count: int):
        """设置输入 token 数（engine 第一次返回时调用）"""
        if token_count > 0:
            self._input_tokens = token_count
            if self._status and self._start_time > 0:
                import time as _time
                elapsed = _time.time() - self._start_time
                try:
                    self._status.update(
                        self._render_status(
                            self._message, elapsed,
                            token_count, self._output_tokens,
                        )
                    )
                except RuntimeError:
                    pass

    def add_tokens(self, token_count: int):
        """增加输出 token 计数（流式输出时调用）"""
        self._streamed = True
        with self._lock:
            self._output_tokens += token_count
        # token 增加时立刻刷新一次显示，不用等 timer
        if self._status and self._start_time > 0:
            import time as _time
            elapsed = _time.time() - self._start_time
            try:
                new_text = self._render_status(
                    self._message, elapsed,
                    self._input_tokens, self._output_tokens,
                )
                with self._lock:
                    self._status.update(new_text)
            except RuntimeError:
                pass

    def stop(self):
        """停止 spinner + 计时线程，记录耗时"""
        import time as _time
        # 停止计时线程
        self._timer_stop.set()
        if self._timer_thread and self._timer_thread.is_alive():
            self._timer_thread.join(timeout=1.0)
        self._timer_thread = None
        # 停止 spinner
        if self._status:
            try:
                self._status.__exit__(None, None, None)
            finally:
                self._status = None
        # 计算总耗时（基于首次启动时间，反映整个请求耗时）
        if self._first_start_time > 0:
            self._elapsed = _time.time() - self._first_start_time
            self._first_start_time = 0.0  # 重置，下次 start 重新开始
        self._start_time = 0.0

    @property
    def elapsed(self) -> float:
        """返回上次思考耗时（秒）"""
        return self._elapsed

    @property
    def output_tokens(self) -> int:
        """返回本次输出 token 数（估算值）"""
        return self._output_tokens

    def __del__(self):
        """析构时确保 spinner 和线程被停止，防止资源泄漏"""
        try:
            self.stop()
        except Exception:
            pass  # 解释器关闭期间状态不可预测，裸 except 是合理的防御手段


async def _run_agent(console, llm, prompt_builder, skills, config, session, user_input: str,
               last_options: list = None,
               permission_callback=None,
               prior_conversation: list = None,
               engine_class=None,
               lsp_client=None,
               code_indexer=None,
               agent_manager=None) -> AgentEngine:
    """运行 Agent 处理用户输入，返回 engine 实例（用于 /undo）

    last_options: 外层传入的列表，AI 返回的选项会写入此列表供数字选择使用
    prior_conversation: 上一轮 engine 的 conversation，用于跨轮次保持上下文记忆
    engine_class: P1-4 Agent 引擎类（默认 AgentEngine = CoderAgentEngine，
                  /explore 等只读命令传 TaskAgentEngine）
    code_indexer: v3.0 CodeIndexer 实例，传入后 4 个语义工具可用且 edit_file 后触发增量索引
    agent_manager: 外部传入的共享 AgentManager（run_interactive 创建），
                  传入后替换 engine 内部默认创建的实例，保证多轮切换状态共享

    改为 async，避免 asyncio.run() 嵌套
    接收 prior_conversation 并赋值给新 engine，避免多轮对话上下文丢失
    """
    # P1-4: 支持双 Agent 类型（Coder/Task），engine_class 默认 None → AgentEngine
    if engine_class is None:
        engine_class = AgentEngine
    engine = engine_class(llm=llm, prompt_builder=prompt_builder, skills=skills, config=config,
                          lsp_client=lsp_client, code_indexer=code_indexer)
    # 注入共享 AgentManager（保留用户启动时已切换的 agent 状态）
    if agent_manager is not None:
        engine._agent_manager = agent_manager
    # 继承上一轮 engine 的 conversation，保持多轮对话上下文
    if prior_conversation:
        engine.conversation = list(prior_conversation)
    spinner = _ThinkingSpinner(console)

    if last_options is None:
        last_options = []
    cancelled = False

    # 设置授权回调 — 用户确认后才执行文件写入和命令执行
    _session_allow_all = {"value": False}  # 会话级全部允许标志（向后兼容）

    def _permission_callback(info: dict):
        # 会话级全部允许（向后兼容旧的全局允许机制）
        if _session_allow_all["value"]:
            return "once"

        action = info.get("action", "")
        target = info.get("target", "")
        details = info.get("details", "")
        spinner.stop()
        console.print()
        console.print(f"  {Symbols.WARN} [bold]授权请求[/bold]")
        console.print(f"    操作: {action}")
        console.print(f"    目标: [bold]{target}[/bold]")
        if details:
            console.print(f"    详情: {details}")
        console.print()
        try:
            # P2-3: 三级审批 — y=允许本次 / a=会话允许 / n=拒绝 / N=永不
            answer = pt_prompt("  允许? (y=允许 / a=会话允许 / n=拒绝 / N=永不): ").strip()
            # N（大写）= 永不，加入黑名单
            if answer == "N":
                console.print(f"  {Symbols.CROSS} 已加入黑名单（永不）", style="red")
                console.print()
                return "never"
            answer_lower = answer.lower()
            if answer_lower == "a":
                console.print(f"  {Symbols.CHECK} 已允许当前会话（此工具不再询问）", style="green")
                console.print()
                return "session"
            if answer_lower in ("y", "yes"):
                console.print()
                return "once"
            # n 或空输入 = 拒绝本次（不持久化）
            console.print()
            return False
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False

    # 非交互模式可注入自动拒绝回调，避免依赖 input()
    if permission_callback is not None:
        engine._permission_callback = permission_callback
    else:
        engine._permission_callback = _permission_callback

    # ask_user 提问回调（同步函数，由 ask_user.execute 用 asyncio.to_thread 调用）
    # 必须是同步函数：内部用阻塞的 pt_prompt，async 函数里直接调用会卡住事件循环，
    # 导致 prompt_toolkit 的 Application.run_async 协程无法被 await（RuntimeWarning）
    def _question_callback(question: str, options: list[str]) -> str:
        spinner.stop()
        console.print()
        console.print(f"  {Symbols.INFO} [bold]AI 提问[/bold]")
        console.print(f"    {question}")
        if options:
            for i, opt in enumerate(options, 1):
                console.print(f"    [cyan]{i}[/cyan]. {opt}")
        console.print(f"    (输入数字选择，或直接输入自定义回答)")
        console.print()
        try:
            answer = pt_prompt("  回答: ").strip()
            # 数字选择
            if answer.isdigit() and options:
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    return options[idx]
            return answer if answer else (options[0] if options else "")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return options[0] if options else ""

    engine._question_callback = _question_callback

    async def process_events():
        nonlocal cancelled
        async for event in engine.process(user_input):
            if _interrupted.is_set():
                cancelled = True
                break
            _handle_event(console, event, session, spinner, last_options)

    try:
        _interrupted.clear()
        await process_events()  # 用 await 代替 asyncio.run()，避免嵌套
    except KeyboardInterrupt:
        # 设置中断标志，使 process_events 中的事件循环能及时退出
        _interrupted.set()
        spinner.stop()
        # 流式渲染器中断时刷新剩余缓冲区
        _md = getattr(spinner, "_md_renderer", None)
        if _md is not None:
            try:
                _md.finish()
            except Exception:
                pass
            spinner._md_renderer = None
        # 兼容旧版 _stream_buffer 逻辑（fallback）
        elif hasattr(spinner, "_stream_buffer") and spinner._stream_buffer:
            buffered_text = "".join(spinner._stream_buffer)
            if buffered_text.strip():
                from rich.markdown import Markdown
                console.print(Markdown(buffered_text))
            spinner._stream_buffer = []
        console.print(f"\n  {Symbols.WARN} 已中断", style="yellow")
        cancelled = True
    except (RuntimeError, OSError, TypeError) as e:
        spinner.stop()
        # 流式渲染器异常时刷新剩余缓冲区
        _md = getattr(spinner, "_md_renderer", None)
        if _md is not None:
            try:
                _md.finish()
            except Exception:
                pass
            spinner._md_renderer = None
        # 兼容旧版 _stream_buffer 逻辑（fallback）
        elif hasattr(spinner, "_stream_buffer") and spinner._stream_buffer:
            buffered_text = "".join(spinner._stream_buffer)
            if buffered_text.strip():
                from rich.markdown import Markdown
                console.print(Markdown(buffered_text))
            spinner._stream_buffer = []
        error_msg = str(e)
        # 注意：以下错误分类基于字符串匹配，理想方案应基于 HTTP 状态码（resp.status_code）。
        # 当前实现保持现状以兼容各后端的错误信息格式。
        if "400" in error_msg:
            ui.show_error(console, "API 请求失败 (400)",
                          "请检查:\n  1. API URL 是否正确\n  2. 模型名称是否存在\n  3. API Key 是否有效\n"
                          f"  详细: {error_msg[:200]}")
        elif "401" in error_msg or "403" in error_msg:
            ui.show_error(console, "API 认证失败 (401/403)",
                          "API Key 无效或已过期。请检查:\n"
                          "  1. API Key 是否正确（启动时显示的 key 前4位是否匹配）\n"
                          "  2. 环境变量 IRON_API_KEY / OPENAI_API_KEY 是否覆盖了 iron.yml 的配置\n"
                          "  3. 重新运行 iron config 或修改 iron.yml 更换 key")
        elif "timeout" in error_msg.lower():
            ui.show_error(console, "API 请求超时", "请检查网络连接，或换一个更快的模型")
        else:
            ui.show_error(console, f"处理失败: {error_msg[:300]}")

    spinner.stop()
    if cancelled:
        console.print(f"  已中断\n", style="dim yellow")

    return engine


def _handle_event(console, event: AgentEvent, session: ConversationSession,
                   spinner: _ThinkingSpinner, last_options: list = None):
    """处理 Agent 引擎产出的事件 — Claude Code 风格

    显示策略：
    - thinking/phase 事件：启动/更新 spinner，不停止
    - file_read/file_done/command/step_done/step_warn 等：一行简短摘要（⎿ 格式）
    - file_code/file_diff：不显示给用户（只记录给 AI 用）
    - plan/questions/summary/chat_response/error：停止 spinner，输出大块内容
    """
    etype = event.type
    data = event.data

    if etype == "thinking":
        # 接收 engine 传来的 input_tokens，显示 ↑ 输入 token 数
        _in_tokens = data.get("input_tokens", 0)
        if _in_tokens and not getattr(spinner, "_input_tokens", 0):
            # 第一次 thinking 事件带 input_tokens，设置进去
            spinner.start(data.get("message", "思考中..."), input_tokens=_in_tokens)
        else:
            spinner.start(data.get("message", "思考中..."))

    elif etype == "phase":
        phase = data.get("phase", "")
        # 精简 phase 标签（不显示"正在..."前缀和"..."后缀）
        phase_labels = {
            Phase.THINK.value: "思考中",
            Phase.EXECUTE.value: "执行中",
            Phase.DONE.value: "完成",
            Phase.CHAT.value: "回复中",
        }
        spinner.update(phase_labels.get(phase, "处理中"))

    elif etype == "step_done":
        # 隐藏步骤完成提示，像 Claude Code 一样静默执行
        pass

    elif etype == "step_warn":
        # 警告仍需显示（用户需要知道有问题）
        console.print(f"  {Symbols.WARN} {data.get('message', '')}", style="yellow")

    elif etype == "skill":
        spinner.stop()
        ui.show_skill_trigger(console, data.get("name", ""), data.get("description", ""))
        spinner.start("继续处理...")

    elif etype == "plan":
        spinner.stop()
        console.print()
        ui.show_plan(
            console,
            files=data.get("files", []),
            description=data.get("description", ""),
            modules=data.get("modules", []),
        )
        spinner.start("准备编码...")

    elif etype == "questions":
        spinner.stop()
        answers = ui.show_questions(console, data.get("questions", []))

    elif etype == "file_start":
        path = data.get("path", "")
        action = data.get("action", "写入")
        # 简洁状态行（不停止 spinner，保持计时连续）
        _fname = Path(path).name if path else ""
        console.print(f"  [dim cyan]⎿ {action}[/dim cyan] {_fname}", highlight=False)
        spinner.update("生成代码中")

    elif etype == "file_code":
        # 不显示完整代码，只记录（给 AI 看的，不是给用户看的）
        path = data.get("path", "")
        code = data.get("code", "")

    elif etype == "file_diff":
        # 不显示完整 diff，只记录
        pass

    elif etype == "file_read":
        # 工具执行可视化（模仿 Claude Code）：读取文件时显示简洁状态行
        # 不停止 spinner（保持思考状态连续）
        path = data.get("path", "")
        if path:
            _fname = Path(path).name if path else path
            console.print(f"  [dim cyan]⎿ 读取[/dim cyan] {_fname}", highlight=False)

    elif etype == "file_tree":
        files = data.get("files", [])
        if files:
            ui.show_file_tree(console, ".", changed_files=files[:10])

    elif etype == "file_fixed":
        fixes = data.get("fixes", 0)
        if fixes > 0:
            console.print(f"  {Symbols.WARN} 自动修复 {fixes} 处问题", style="yellow")

    elif etype == "file_done":
        path = data.get("path", "")
        lines = data.get("lines", 0)
        _fname = Path(path).name if path else path
        console.print(f"  [dim green]✓ 写入[/dim green] [bold]{_fname}[/bold] ({lines} 行)")
        session.add_message("assistant", f"生成文件: {path} ({lines} 行)")

    elif etype == "command":
        cmd = data.get("command", "")
        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        returncode = data.get("returncode", 0)
        # 简短显示：命令 + 结果
        if returncode == 0:
            # 成功：只显示命令和输出行数
            out_lines = len(stdout.strip().split("\n")) if stdout.strip() else 0
            console.print(f"  [dim green]✓[/dim green] [dim]{cmd[:60]}[/dim] ({out_lines} 行输出)")
        else:
            # 失败：显示命令和错误摘要
            err_summary = stderr.strip().split("\n")[-1][:80] if stderr.strip() else ""
            console.print(f"  [red]✗[/red] [bold]{cmd[:60]}[/bold] [red]退出码 {returncode}[/red]")
            if err_summary:
                console.print(f"    [dim red]{err_summary}[/dim red]")

    elif etype == "permission_request":
        # 授权请求由 _permission_callback 处理，这里不重复显示
        pass

    elif etype == "findings":
        spinner.stop()
        findings = data.get("findings", [])
        if findings:
            ui.show_findings(console, findings)
        spinner.start("继续处理...")

    elif etype == "summary":
        spinner.stop()
        console.print()
        ui.show_summary(
            console,
            files_created=data.get("files_created", []),
            files_modified=data.get("files_modified", []),
            findings_fixed=data.get("findings_fixed", 0),
            findings_remaining=data.get("findings_remaining", 0),
        )
        # P3 修复（第七轮）：移除任务完成后的选项菜单弹出
        # 用户反馈：完成之后立马弹出选择窗口没必要，像 Claude Code 一样直接结束即可
        # 用户可通过 /build 等命令继续操作，无需强制选择
        if last_options is not None:
            last_options.clear()

    elif etype == "chat_chunk":
        # 流式输出文本增量：实时用 MarkdownStreamRenderer 渲染
        # 首次 chunk 时停止 spinner 并创建渲染器；后续 chunk 直接 append
        text = data.get("text", "")
        if text:
            if not getattr(spinner, "_md_renderer", None):
                spinner.stop()
                console.print()
                from iron.cli.ui import MarkdownStreamRenderer
                spinner._md_renderer = MarkdownStreamRenderer(console)
            spinner._md_renderer.append(text)
            spinner._streamed = True
            _est_tokens = _count_output_tokens(text)
            spinner.add_tokens(_est_tokens)

    elif etype == "chat_response":
        spinner.stop()
        message = data.get("message", "")
        # 流式模式：用 MarkdownStreamRenderer 渲染（finish 刷新剩余缓冲区）
        if getattr(spinner, "_streamed", False) and getattr(spinner, "_md_renderer", None):
            _renderer = spinner._md_renderer
            try:
                _renderer.finish()
            except Exception:
                # 渲染异常时 fallback 到纯文本
                _full_text = _renderer.get_full_text() or message
                if _full_text.strip():
                    for _line in _full_text.split("\n"):
                        console.print(f"  {_line}")
            spinner._md_renderer = None
            spinner._streamed = False
        elif getattr(spinner, "_streamed", False):
            # 兼容旧版 _stream_buffer 逻辑（fallback）
            _buf = getattr(spinner, "_stream_buffer", [])
            _full_text = "".join(_buf) if _buf else message
            console.print()
            if _full_text.strip():
                try:
                    from rich.markdown import Markdown
                    console.print(Markdown(_full_text))
                except (ValueError, TypeError):
                    for line in _full_text.split("\n"):
                        console.print(f"  {line}")
            spinner._stream_buffer = []
            spinner._streamed = False
        else:
            # 非流式 fallback（如 EchoBackend）
            console.print()
            if message.strip():
                try:
                    from rich.markdown import Markdown
                    console.print(Markdown(message))
                except (ValueError, TypeError):
                    for line in message.split("\n"):
                        console.print(f"  {line}")
        # P2 修复（第七轮）：显示思考耗时 + token 统计
        _elapsed = spinner.elapsed
        if _elapsed > 0 or spinner.output_tokens > 0:
            if _elapsed < 60:
                _time_str = f"{_elapsed:.1f}s"
            else:
                _mins = int(_elapsed // 60)
                _secs = _elapsed % 60
                _time_str = f"{_mins}m{_secs:.0f}s"
            _parts = [f"⏱ {_time_str}"]
            if spinner.output_tokens > 0:
                _parts.append(f"↓ {spinner.output_tokens} tokens")
            console.print(f"  [dim]{' · '.join(_parts)}[/dim]")

    elif etype == "error":
        spinner.stop()
        ui.show_error(console, data.get("message", "未知错误"))


# ── 辅助命令 ────────────────────────────────────────────────────

def _do_undo(console: Console, engine: AgentEngine):
    """执行撤销操作"""
    # Bug 修复（第六轮）：复用会话事件循环，避免 httpx 客户端跨循环
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(engine.undo_last())
    if result is None:
        console.print(f"  {Symbols.WARN} 没有可撤销的操作", style="dim yellow")
        return

    path = result.get("path", "")
    action = result.get("action", "")
    console.print(f"\n  {Symbols.CHECK} 已撤销: [bold]{path}[/bold] ({action})", style="green")
    if action == "新建":
        console.print(f"  {Symbols.CROSS} 已删除文件", style="dim")
    elif action == "修改":
        console.print(f"  {Symbols.FILE_EDIT} 已恢复原始内容", style="dim")
    console.print()


def _show_rules(prompt_builder: PromptBuilder, config: IronConfig):
    """显示当前生效的编码规则"""
    iron_count, ai_count, proj_count = prompt_builder.count_active_rules()
    console.print(f"\n  {Symbols.SHIELD} 编码规则 ({iron_count + ai_count + proj_count} 条)\n")
    console.print(f"    [bold cyan]Layer 1[/bold cyan] 嵌入式铁律: {iron_count} 条 (不可关闭)")
    for name in IRON_RULE_NAMES:
        console.print(f"      {Symbols.DIAMOND} {name}")
    console.print(f"\n    [bold cyan]Layer 2[/bold cyan] AI 反模式: {ai_count} 条")
    console.print(f"\n    [bold cyan]Layer 3[/bold cyan] 项目规则: {proj_count} 条 (.iron-agent/rules/)")
    console.print()


def _show_config(config: IronConfig):
    """显示当前配置"""
    console.print(f"\n  {Symbols.WRENCH} 当前配置\n")
    console.print(f"    LLM 后端:  {config.llm.backend}")
    console.print(f"    模型:      {config.llm.model}")
    console.print(f"    MCU:       {config.project.mcu}")
    console.print(f"    语言:      {config.project.language}")
    console.print(f"    框架:      {config.project.framework}")
    console.print(f"    构建系统:  {config.project.build_system}")
    console.print(f"    最大修复:  {config.max_fix_rounds} 轮")
    console.print()


def _show_history(project_root: Path):
    """显示历史会话"""
    sessions_dir = Path.home() / ".iron" / "sessions"
    sessions = ConversationSession.list_sessions(sessions_dir)
    if not sessions:
        console.print(f"\n  {Symbols.INFO} 暂无历史会话\n", style="dim")
        return
    console.print(f"\n  {Symbols.INFO} 历史会话 ({len(sessions)} 个)\n")
    for s in sessions[:10]:
        console.print(f"    {s['id']}  MCU: {s['mcu']}  消息: {s['messages']}  {s['created_at']}")
    console.print()


def _resume_session(project_root: Path, session_id: str = "") -> ConversationSession | None:
    """恢复历史会话

    用法：
    - /resume <id>  恢复指定会话
    - /resume       列出会话让用户选择
    """
    sessions_dir = Path.home() / ".iron" / "sessions"
    sessions = ConversationSession.list_sessions(sessions_dir)
    if not sessions:
        console.print(f"\n  {Symbols.INFO} 暂无历史会话\n", style="dim")
        return None

    if session_id:
        # 精确匹配或前缀匹配
        for s in sessions:
            if s["id"] == session_id or s["id"].startswith(session_id):
                session_file = sessions_dir / f"{s['id']}.json"
                return ConversationSession.load(session_file)
        console.print(f"  {Symbols.WARN} 未找到会话: {session_id}", style="yellow")
        return None

    # 无参数：列出会话让用户选择
    console.print(f"\n  {Symbols.INFO} 历史会话 ({len(sessions)} 个)\n")
    for i, s in enumerate(sessions[:10], 1):
        console.print(f"    {i}. {s['id']}  MCU: {s['mcu']}  消息: {s['messages']}  {s['created_at']}")

    try:
        choice = pt_prompt("\n  选择会话编号（回车取消）: ").strip()
        if not choice or not choice.isdigit():
            return None
        idx = int(choice) - 1
        if 0 <= idx < len(sessions[:10]):
            s = sessions[idx]
            session_file = sessions_dir / f"{s['id']}.json"
            return ConversationSession.load(session_file)
    except (ValueError, KeyboardInterrupt, EOFError):
        pass
    return None


def _switch_model(config: IronConfig) -> str | None:
    """两阶段模型选择：先选厂商（如多于一个），再选模型（上下键）

    流程：
    1. 如果配置中没有 providers，走旧逻辑（兼容）
    2. 如果只有一个 provider，跳过厂商选择
    3. 多于一个 provider：用上下键选择厂商
    4. 选中厂商后用上下键选择模型
    5. 提交：更新 provider 和 active_provider，同步到 llm 字段

    Returns:
        选中的模型名（str）或 None（取消）
    """
    from prompt_toolkit import prompt as pt_prompt
    from iron.cli.ui import select_with_arrows

    # 没有 providers：走旧逻辑（兼容）
    if not config.providers:
        return _switch_model_legacy(config)

    # 阶段 1：选厂商（只有一个则跳过）
    if len(config.providers) == 1:
        provider = config.providers[0]
    else:
        # 构造厂商选项列表
        active_name = config.active_provider or (config.providers[0].name if config.providers else "")
        provider_options = []
        default_provider_idx = 0
        for i, p in enumerate(config.providers):
            key_icon = "✓" if p.api_key else "⚠"
            desc = f"{key_icon} {p.name} ({p.backend}) — {p.model}"
            if p.name == active_name:
                desc += "  ◄ 当前"
                default_provider_idx = i
            provider_options.append((p, desc))

        provider = select_with_arrows(
            provider_options,
            title="选择厂商",
            default_idx=default_provider_idx,
            console=console,
        )
        if provider is None:
            console.print(f"  {Symbols.WARN} 已取消", style="dim yellow")
            return None

    # 阶段 2：选模型
    available = provider.available_models or []
    if not available:
        # 尝试重新扫描
        console.print(f"\n  {Symbols.MAGNIFY} 扫描 {provider.name} 可用模型...", style="dim")
        available = IronConfig.fetch_available_models(
            provider.base_url, provider.api_key, provider.backend
        )
        if available:
            provider.available_models = available

    if not available:
        # fallback 手动输入
        model = pt_prompt(f"  {provider.name} 模型名称: ", default=provider.model or "gpt-4o")
        selected_model = _sanitize_model(model, provider.model or "gpt-4o")
    else:
        # 用上下键选择模型
        model_options = [(m, m) for m in available]
        default_model_idx = 0
        if provider.model in available:
            default_model_idx = available.index(provider.model)

        selected_model = select_with_arrows(
            model_options,
            title=f"选择 {provider.name} 模型",
            default_idx=default_model_idx,
            console=console,
        )
        if selected_model is None:
            console.print(f"  {Symbols.WARN} 已取消", style="dim yellow")
            return None

    # 提交：更新 provider 和 config
    provider.model = selected_model
    config.active_provider = provider.name
    # 同步 active provider 到 llm 字段（重建 backend 会用到）
    config._apply_active_provider_to_llm()

    return selected_model


def _switch_model_legacy(config: IronConfig) -> str | None:
    """旧逻辑：单厂商按序号/名称选择（向后兼容）"""
    from prompt_toolkit import prompt as pt_prompt
    from iron.cli.ui import select_with_arrows

    available = config.llm.available_models or []
    if not available:
        console.print(f"\n  {Symbols.MAGNIFY} 扫描可用模型...", style="dim")
        available = IronConfig.fetch_available_models(
            config.llm.base_url, config.llm.api_key, config.llm.backend
        )
        if available:
            config.llm.available_models = available
            config.save()

    if not available:
        console.print(f"  {Symbols.WARN} 未获取到模型列表，请手动输入", style="yellow")
        model = pt_prompt("  模型名称: ", default=config.llm.model)
        return _sanitize_model(model, config.llm.model)

    # 用上下键选择
    model_options = [(m, m) for m in available]
    default_idx = 0
    if config.llm.model in available:
        default_idx = available.index(config.llm.model)

    selected = select_with_arrows(
        model_options,
        title=f"选择模型 (当前: {config.llm.model})",
        default_idx=default_idx,
        console=console,
    )
    if selected is None:
        console.print(f"  {Symbols.WARN} 已取消", style="dim yellow")
        return None
    return selected


def _sanitize_model(model_input: str, fallback: str) -> str:
    """验证并清理模型名"""
    model = model_input.strip()
    if not model:
        return fallback
    # 如果输入是纯数字，可能是用户误输入了序号
    if model.isdigit():
        console.print(f"  {Symbols.WARN} 模型名不能是纯数字，请输入模型名称", style="yellow")
        return fallback
    # 去除中文逗号等异常字符
    if "，" in model or "。" in model:
        model = model.replace("，", ",").replace("。", ".")
        console.print(f"  {Symbols.WARN} 已修正模型名中的中文标点", style="yellow")
    return model


# ── 单次编码模式 ────────────────────────────────────────────────

async def run_single(config: IronConfig, project_root: Path, prompt: str):
    """单次编码模式（非交互）

    遗留4 修复：非 TTY 环境（管道/重定向）用纯文本输出，避免 Rich 面板字符污染
    """
    # 检测是否在管道/重定向中（非 TTY），用无样式 Console
    # 非交互场景下终端宽度不可探测，固定 120 列以保证可读性
    if not sys.stdout.isatty():
        run_console = Console(no_color=True, highlight=False, width=120)
    else:
        run_console = console

    prompt_builder = PromptBuilder(project_root, config.project.mcu)
    skills = SkillRegistry()

    try:
        llm = create_backend(config.llm.backend, config)
    except ImportError:
        from iron.llm.backend import EchoBackend
        llm = EchoBackend()

    session = ConversationSession(mcu=config.project.mcu, project_dir=str(project_root))
    # 非交互模式默认拒绝所有授权请求，避免依赖 input()
    await _run_agent(run_console, llm, prompt_builder, skills, config, session, prompt,
                    permission_callback=lambda info: False)

    # 保存会话
    try:
        sessions_dir = Path.home() / ".iron" / "sessions"
        session.save(sessions_dir)
    except OSError as e:
        run_console.print(f"  ⚠ 会话保存失败: {e}", style="dim yellow")


if __name__ == "__main__":
    cli()
