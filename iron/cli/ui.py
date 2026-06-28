"""Iron CLI 核心 UI 模块 — 提供终端动态交互界面

本模块是 Iron 嵌入式 AI 开发 Agent 的核心 UI 层，基于 rich 和 prompt_toolkit
构建，提供面板、代码高亮、思考动画、进度追踪、命令补全等全套终端 UI 组件。
"""

from __future__ import annotations

import difflib
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PtStyle
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit import PromptSession
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.keys import Keys

from iron.cli.theme import Colors, Symbols, PanelTitles, IRON_RULE_NAMES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 样式常量
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class StyleConstants:
    """全局 UI 样式常量，统一视觉风格"""

    PANEL_BORDER = "cyan"
    SEPARATOR = "dim cyan"
    SUCCESS = "green"
    WARNING = "yellow"
    ERROR = "red"
    INPUT_PREFIX = "[bold cyan]>[/bold cyan] "
    STEP_OK = "[green]✓[/green]"
    STEP_WARN = "[yellow]⚠[/yellow]"
    STEP_FAIL = "[red]✗[/red]"
    STEP_THINK = "[cyan]⏳[/cyan]"
    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# prompt_toolkit 补全菜单样式 — 确保中文描述清晰
_PT_STYLE = PtStyle.from_dict({
    "prompt": "bold cyan",
    "completion-menu": "bg:#1e1e1e fg:#ffffff",
    "completion-menu.completion": "fg:#ffffff",
    "completion-menu.completion.current": "bg:#005f87 fg:#ffffff bold",
    "completion-menu.meta.completion": "fg:#cccccc",
    "completion-menu.meta.completion.current": "bg:#005f87 fg:#ffffff",
})


# 文件扩展名 → rich Syntax 语言名映射
_EXT_LANG_MAP: dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".py": "python",
    ".json": "json",
    ".cmake": "cmake",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".txt": "text",
    ".ld": "c",
    ".s": "asm",
    ".S": "asm",
    ".asm": "asm",
    ".sh": "bash",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".js": "javascript",
    ".ts": "typescript",
}

# 文件树忽略目录
_IGNORE_DIRS: set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".iron-agent",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "build",
    ".cache",
}


def _detect_language(path: Path) -> str:
    """根据文件扩展名自动检测编程语言

    Args:
        path: 文件路径

    Returns:
        rich Syntax 支持的语言名称
    """
    # Makefile 特殊处理
    name_upper = path.name.upper()
    if name_upper in ("MAKEFILE", "GNUMAKEFILE"):
        return "makefile"

    ext = path.suffix.lower()
    return _EXT_LANG_MAP.get(ext, "text")


def _get_terminal_width() -> int:
    """获取终端宽度，失败时回退到 80"""
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 欢迎界面
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_welcome(
    console: Console,
    version: str,
    mcu: str,
    model: str,
    project_dir: str,
    rules_count: int,
    build_system: str,
) -> None:
    """显示欢迎横幅和环境信息 — 简洁风格

    Args:
        console: rich Console 实例
        version: 版本号
        mcu: MCU 型号
        model: 使用的 AI 模型名称
        rules_count: 已加载的编码规则数量
        build_system: 构建系统名称
        project_dir: 项目目录路径
    """
    ascii_art = (
        "[bold cyan]"
        "  ██╗██████╗  ██████╗ ███╗   ██╗\n"
        "  ██║██╔══██╗██╔═══██╗████╗  ██║\n"
        "  ██║██████╔╝██║   ██║██╔██╗ ██║\n"
        "  ██║██╔══██╗██║   ██║██║╚██╗██║\n"
        "  ██║██║  ██║╚██████╔╝██║ ╚████║\n"
        "  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝"
        "[/bold cyan]"
    )

    title_text = Text()
    title_text.append(f"  v{version}", style="dim cyan")
    title_text.append("   ", style="default")
    title_text.append("嵌入式 AI 开发 Agent", style="bold cyan")

    status_table = Table(
        show_header=False,
        box=None,
        padding=(0, 2),
        expand=True,
    )
    status_table.add_column(justify="right", style="dim", min_width=12)
    status_table.add_column(style="bright_white")

    status_table.add_row(f"{Symbols.DIAMOND} MCU", mcu)
    status_table.add_row(f"{Symbols.BRAIN} 模型", model)
    status_table.add_row(f"{Symbols.SHIELD} 规则", f"{rules_count} 条")
    status_table.add_row(f"{Symbols.WRENCH} 构建", build_system)
    status_table.add_row(f"{Symbols.FOLDER} 项目", project_dir)

    content = Group(ascii_art, Text(""), title_text, Rule(style="dim cyan"), status_table)

    panel = Panel(
        content,
        border_style=StyleConstants.PANEL_BORDER,
        padding=(1, 2),
    )
    console.print()
    console.print(panel)
    console.print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 状态栏
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_status_bar(
    console: Console,
    mcu: str,
    model: str,
    rules_count: int,
    build_system: str,
    agent: str = "",
) -> None:
    """显示单行状态栏

    Args:
        console: rich Console 实例
        mcu: MCU 型号
        model: AI 模型名称
        rules_count: 规则数量
        build_system: 构建系统
        agent: 当前 agent 名（非空时显示）
    """
    items = [
        Text(f"MCU: {mcu}", style="cyan"),
        Text(f"规则: {rules_count} 条活跃", style="green"),
        Text(f"模型: {model}", style="blue"),
        Text(f"构建: {build_system}", style="magenta"),
    ]
    if agent:
        items.append(Text(f"Agent: {agent}", style="yellow"))
    columns = Columns(
        items,
        separator=" │ ",
        expand=True,
        padding=(0, 1),
    )
    console.print(columns)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 思考过程动画
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ThinkingDisplay:
    """显示 AI 思考过程的动画面板

    用作上下文管理器，内部维护一个 rich Live 实时刷新区域。
    每调用 step/step_done/step_warn 追加一行思考步骤。

    用法:
        with ThinkingDisplay(console) as think:
            think.step("分析需求...")
            think.step_done("意图: UART1 驱动 + DMA 接收")
            think.step_warn("技能: [driver-gen] 外设驱动生成")
    """

    def __init__(self, console: Console, title: str = "理解") -> None:
        self._console = console
        self._title = title
        self._steps: list[tuple[str, str]] = []  # (icon_markup, text)
        self._live: Optional[Live] = None

    # -- 类方法：快速显示阶段标题 --

    @classmethod
    def start_phase(cls, console: Console, title: str) -> None:
        """打印一个阶段标题行（不使用 Live 动画）"""
        console.print(f"\n  {Symbols.DIAMOND} [bold cyan]{title}[/bold cyan]")

    # -- 上下文管理器 --

    def __enter__(self) -> "ThinkingDisplay":
        self._live = Live(
            self._build_renderable(),
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._live is not None:
            # 最后刷新一次，确保最终状态完整显示
            self._live.update(self._build_renderable())
            self._live.__exit__(exc_type, exc_val, exc_tb)

    # -- 步骤控制 --

    def step(self, text: str) -> None:
        """添加一个正在进行的思考步骤（显示旋转动画）"""
        self._steps.append((StyleConstants.STEP_THINK, text))
        self._refresh()

    def step_done(self, text: str) -> None:
        """将最后一步标记为完成"""
        if self._steps:
            self._steps[-1] = (StyleConstants.STEP_OK, text)
        self._refresh()

    def step_warn(self, text: str) -> None:
        """将最后一步标记为警告"""
        if self._steps:
            self._steps[-1] = (StyleConstants.STEP_WARN, text)
        self._refresh()

    def step_fail(self, text: str) -> None:
        """将最后一步标记为失败"""
        if self._steps:
            self._steps[-1] = (StyleConstants.STEP_FAIL, text)
        self._refresh()

    def add_step(self, icon: str, text: str) -> None:
        """直接追加一行（不替换最后一步）"""
        self._steps.append((icon, text))
        self._refresh()

    # -- 内部方法 --

    def _build_renderable(self) -> Panel:
        """构建当前状态的可渲染对象"""
        lines = []
        for icon, text in self._steps:
            line = Text()
            line.append(f"  {icon} ", style="default")
            line.append(text, style="default")
            lines.append(line)

        content = Group(*lines) if lines else Text("  正在准备...", style="dim")
        return Panel(
            content,
            border_style=StyleConstants.PANEL_BORDER,
            title=self._title,
            title_align="left",
            padding=(0, 1),
        )

    def _refresh(self) -> None:
        """刷新 Live 显示"""
        if self._live is not None:
            self._live.update(self._build_renderable())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 执行计划展示
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_plan(
    console: Console,
    files: list[dict],
    description: str,
    modules: list[str],
) -> None:
    """显示执行计划面板

    Args:
        console: rich Console 实例
        files: 文件操作列表，每项包含 path、action、description
        description: 计划描述
        modules: 模块设计要点列表

    文件列表示例:
        [{"path": "src/uart_dma.c", "action": "新建", "description": "DMA 驱动实现"}]
    模块示例:
        ["UART1_DMA_Init() — GPIO + UART + DMA + IDLE 中断", ...]
    """
    # 文件树
    tree = Tree(
        f"[bold cyan]{Symbols.FOLDER} 文件计划[/bold cyan]",
        guide_style="cyan",
    )
    for idx, f in enumerate(files):
        path = f.get("path", "")
        action = f.get("action", "")
        desc = f.get("description", "")

        # 根据 action 选择图标
        action_icons = {
            "新建": Symbols.FILE_NEW,
            "新建文件": Symbols.FILE_NEW,
            "编辑": Symbols.FILE_EDIT,
            "修改": Symbols.FILE_EDIT,
            "删除": Symbols.CROSS,
        }
        icon = action_icons.get(action, Symbols.ARROW)

        label = Text()
        label.append(f" {icon} ", style="default")
        label.append(f"{path} ", style="bright_white")
        label.append(f"[{action}]", style="dim cyan")
        if desc:
            label.append(f"  {Symbols.ARROW} {desc}", style="dim")

        tree.add(label)

    # 模块设计
    module_lines = []
    for m in modules:
        line = Text()
        line.append(f"  {Symbols.BULLET} ", style="cyan")
        line.append(m, style="default")
        module_lines.append(line)

    module_content = Group(*module_lines) if module_lines else Text("  （无模块设计）", style="dim")

    # 计划描述
    desc_text = Text()
    desc_text.append(f"  {description}", style="default")

    content = Group(desc_text, Text(""), tree, Text(""), Rule(style="dim cyan"), Text(""), module_content)

    panel = Panel(
        content,
        border_style=StyleConstants.PANEL_BORDER,
        title=PanelTitles.PLAN,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 交互式提问
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_questions(console: Console, questions: list[dict]) -> list[str]:
    """显示交互式问题面板并收集用户回答

    Args:
        console: rich Console 实例
        questions: 问题列表，每项包含 prompt、options、default

    Returns:
        用户回答列表

    问题格式:
        {"prompt": "使用哪种 DMA 模式?", "options": ["循环模式", "普通模式"], "default": "循环模式"}
    """
    if not questions:
        return []

    # 显示问题面板
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column(style="cyan", min_width=4)
    table.add_column(style="bright_white")

    for idx, q in enumerate(questions, 1):
        prompt_text = q.get("prompt", "")
        options = q.get("options", [])
        default = q.get("default", "")

        question_text = Text()
        question_text.append(prompt_text, style="bold bright_white")
        if default:
            question_text.append(f"  (默认: {default})", style="dim")
        if options:
            question_text.append("\n")
            for oi, opt in enumerate(options, 1):
                question_text.append(f"    {oi}. {opt}\n", style="default")

        table.add_row(f"  {idx}.", question_text)

    panel = Panel(
        table,
        border_style=StyleConstants.PANEL_BORDER,
        title=PanelTitles.CLARIFY,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)

    # 收集回答
    answers: list[str] = []
    completer = WordCompleter(
        [opt for q in questions for opt in q.get("options", [])],
        ignore_case=True,
    )

    for idx, q in enumerate(questions, 1):
        prompt_text = q.get("prompt", "")
        options = q.get("options", [])
        default = q.get("default", "")

        # 构建提示文本
        hint = f" ({'/'.join(options)})" if options else ""
        input_prompt = f"  [{idx}/{len(questions)}] {prompt_text}{hint}: "

        try:
            answer = pt_prompt(
                input_prompt,
                completer=completer if options else None,
                default=default,
                style=_PT_STYLE,
            )
            answers.append(answer.strip() if answer.strip() else default)
        except (EOFError, KeyboardInterrupt):
            answers.append(default)

    console.print()
    return answers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. 代码展示
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CodeDisplay:
    """语法高亮代码展示器"""

    @staticmethod
    def show_code(
        console: Console,
        filename: str,
        code: str,
        language: str = "c",
        highlights: list[int] | None = None,
    ) -> None:
        """显示带语法高亮的代码

        Args:
            console: rich Console 实例
            filename: 文件名（显示为面板标题）
            code: 代码内容
            language: 编程语言，用于选择高亮规则
            highlights: 需要高亮强调的行号列表
        """
        syntax = Syntax(
            code,
            language,
            theme="monokai",
            line_numbers=True,
            word_wrap=True,
            highlight_lines=set(highlights) if highlights else set(),
            padding=(1, 0),
        )

        # 文件扩展名映射
        lang_map = {
            "c": "C",
            "h": "C Header",
            "cpp": "C++",
            "py": "Python",
            "json": "JSON",
            "cmake": "CMake",
            "ini": "INI",
            "yaml": "YAML",
            "makefile": "Makefile",
            "ld": "Linker Script",
        }
        lang_label = lang_map.get(language, language.upper())

        title = Text()
        title.append(f" {Symbols.FILE_NEW} ", style="default")
        title.append(filename, style="bright_white")
        title.append(f"  ({lang_label})", style="dim")

        panel = Panel(
            syntax,
            border_style=StyleConstants.PANEL_BORDER,
            title=title,
            title_align="left",
            padding=(0, 1),
        )
        console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. 文件生成进度追踪
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ProgressTracker:
    """多文件生成进度追踪器

    在终端中以树形结构显示每个文件的生成状态。
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._started = False

    def start_file(self, filename: str) -> None:
        """显示文件开始生成"""
        if not self._started:
            self._console.print(f"  [dim]{PanelTitles.IMPLEMENT}[/dim]")
            self._started = True

        text = Text()
        text.append(f"  {Symbols.TREE_BRANCH} ", style="cyan")
        text.append(f"{filename}", style="bright_white")
        text.append(f"  {Symbols.HAMMER} 生成中...", style="dim yellow")
        self._console.print(text)

    def complete_file(self, filename: str, lines: int, fixes: int = 0) -> None:
        """显示文件生成完成"""
        text = Text()
        text.append(f"  {Symbols.TREE_BRANCH} ", style="cyan")
        text.append(f"{filename}", style="bright_white")
        text.append(f"  {Symbols.CHECK} ", style="green")
        text.append(f"{lines} 行", style="green")
        if fixes > 0:
            text.append(f"  (自动修复 {fixes} 处)", style="dim yellow")
        self._console.print(text)

    def fail_file(self, filename: str, error: str) -> None:
        """显示文件生成失败"""
        text = Text()
        text.append(f"  {Symbols.TREE_BRANCH} ", style="cyan")
        text.append(f"{filename}", style="bright_white")
        text.append(f"  {Symbols.CROSS} 错误: ", style="red")
        text.append(error, style="red")
        self._console.print(text)

    def finish(self) -> None:
        """显示追踪结束标记"""
        self._console.print(f"  [cyan]{Symbols.TREE_LAST}[/cyan] [dim]完成[/dim]")
        self._started = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. EmbedGuard 静态分析结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_findings(console: Console, findings: list[dict]) -> None:
    """显示 EmbedGuard 静态分析结果表格，并在下方显示代码片段预览

    Args:
        console: rich Console 实例
        findings: 分析结果列表，每项包含 rule_id、severity、line、message、auto_fixable，
                  可选包含 code（代码片段）、file（文件路径）

    结果格式:
        [{"rule_id": "IRON-001", "severity": "error", "line": 42,
          "message": "检测到动态内存分配 malloc()", "auto_fixable": True,
          "code": "void *p = malloc(100);", "file": "src/main.c"}]
    """
    if not findings:
        console.print(f"  {Symbols.CHECK} [green]未发现问题，代码符合规范[/green]")
        return

    table = Table(
        title="EmbedGuard 分析结果",
        border_style=StyleConstants.PANEL_BORDER,
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("规则 ID", style="cyan", min_width=10)
    table.add_column("严重性", justify="center", min_width=8)
    table.add_column("行号", justify="right", style="dim", min_width=5)
    table.add_column("信息", style="bright_white", ratio=1)
    table.add_column("可修复", justify="center", min_width=6)

    severity_styles = {
        "error": "bold red",
        "warning": "yellow",
        "info": "blue",
    }
    severity_labels = {
        "error": "错误",
        "warning": "警告",
        "info": "提示",
    }

    for f in findings:
        rule_id = f.get("rule_id", "")
        severity = f.get("severity", "info")
        line = f.get("line", "")
        message = f.get("message", "")
        auto_fixable = f.get("auto_fixable", False)

        style = severity_styles.get(severity, "default")
        label = severity_labels.get(severity, severity)

        table.add_row(
            rule_id,
            Text(label, style=style),
            str(line),
            message,
            Symbols.CHECK if auto_fixable else "—",
        )

    console.print(table)

    # ── 代码片段预览 ──
    # 在表格下方显示有问题的代码行（带行号高亮）
    code_snippets: list[Panel] = []
    for f in findings:
        code = f.get("code")
        if not code:
            continue

        line = f.get("line")
        rule_id = f.get("rule_id", "")
        message = f.get("message", "")
        file_path = f.get("file", "")
        severity = f.get("severity", "info")

        # 检测语言
        if file_path:
            lang = _detect_language(Path(file_path))
        else:
            lang = "c"

        # 高亮问题行
        highlight_lines: set[int] = set()
        if line and isinstance(line, int) and line > 0:
            highlight_lines = {line}

        syntax = Syntax(
            code,
            lang,
            theme="monokai",
            line_numbers=True,
            word_wrap=True,
            highlight_lines=highlight_lines,
            padding=(0, 0),
        )

        # 构建面板标题
        title = Text()
        title.append(f" {Symbols.MAGNIFY} ", style="default")
        if file_path:
            title.append(f"{file_path}", style="bright_white")
            title.append(f":{line}" if line else "", style="dim")
        else:
            title.append(f"{rule_id}", style="cyan")
        title.append(f"  {message}", style="dim")

        # 根据严重性选择边框颜色
        border = severity_styles.get(severity, StyleConstants.PANEL_BORDER)
        if "red" in border:
            border = "red"
        elif "yellow" in border:
            border = "yellow"
        elif "blue" in border:
            border = "blue"
        else:
            border = StyleConstants.PANEL_BORDER

        snippet_panel = Panel(
            syntax,
            border_style=border,
            title=title,
            title_align="left",
            padding=(0, 0),
        )
        code_snippets.append(snippet_panel)

    if code_snippets:
        console.print()
        for snippet in code_snippets:
            console.print(snippet)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. 技能触发通知
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_skill_trigger(console: Console, skill_name: str, description: str) -> None:
    """显示技能触发通知

    Args:
        console: rich Console 实例
        skill_name: 技能名称
        description: 技能描述
    """
    text = Text()
    text.append(f"  {Symbols.CHECK} ", style="green")
    text.append("触发技能: ", style="default")
    text.append(f"[{skill_name}]", style="bold cyan")
    text.append(f" {description}", style="default")
    console.print(text)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10. 完成摘要
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_summary(
    console: Console,
    files_created: list[str],
    files_modified: list[str],
    findings_fixed: int,
    findings_remaining: int,
) -> None:
    """显示完成摘要面板

    Args:
        console: rich Console 实例
        files_created: 新建文件列表
        files_modified: 修改文件列表
        findings_fixed: 已自动修复的问题数
        findings_remaining: 剩余问题数
    """
    total_files = len(files_created) + len(files_modified)

    # 摘要统计
    stats = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats.add_column(justify="right", style="dim", min_width=10)
    stats.add_column(style="bright_white")

    stats.add_row(f"{Symbols.CHECK} 新建文件", f"{len(files_created)} 个")
    stats.add_row(f"{Symbols.FILE_EDIT} 修改文件", f"{len(files_modified)} 个")
    stats.add_row(f"{Symbols.SHIELD} 修复问题", f"{findings_fixed} 处")

    if findings_remaining > 0:
        stats.add_row(f"{Symbols.WARN} 剩余问题", Text(f"{findings_remaining} 处", style="yellow"))
    else:
        stats.add_row(f"{Symbols.DONE} 全部通过", Text("无剩余问题", style="green"))

    # 文件列表
    file_lines = []
    for f in files_created:
        line = Text()
        line.append(f"  {Symbols.TREE_BRANCH} ", style="cyan")
        line.append(f"{Symbols.FILE_NEW} ", style="default")
        line.append(f, style="bright_white")
        line.append("  [新建]", style="dim green")
        file_lines.append(line)

    for f in files_modified:
        line = Text()
        line.append(f"  {Symbols.TREE_LAST} ", style="cyan")
        line.append(f"{Symbols.FILE_EDIT} ", style="default")
        line.append(f, style="bright_white")
        line.append("  [修改]", style="dim blue")
        file_lines.append(line)

    file_list = Group(*file_lines) if file_lines else Text("  （无文件变更）", style="dim")

    content = Group(
        Text(f"  {Symbols.ROCKET} 任务完成！共处理 {total_files} 个文件", style="bold green"),
        Text(""),
        stats,
        Text(""),
        Rule(style="dim cyan"),
        file_list,
    )

    panel = Panel(
        content,
        border_style="green",
        title=PanelTitles.SUMMARY,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11. 错误显示
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_error(console: Console, message: str, details: str = "") -> None:
    """显示错误信息面板

    Args:
        console: rich Console 实例
        message: 错误信息
        details: 详细错误描述
    """
    text = Text()
    text.append(f"  {Symbols.CROSS} ", style="bold red")
    text.append(message, style="bold red")

    content_lines = [text]
    if details:
        content_lines.append(Text(""))
        detail_text = Text()
        detail_text.append(f"  {details}", style="dim")
        content_lines.append(detail_text)

    panel = Panel(
        Group(*content_lines),
        border_style="red",
        title=PanelTitles.ERROR,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12. 帮助信息
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_help(console: Console) -> None:
    """显示所有可用命令的帮助表格"""
    table = Table(
        title="可用命令",
        border_style=StyleConstants.PANEL_BORDER,
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("命令", style="bold cyan", min_width=14)
    table.add_column("描述", style="default", ratio=1)

    commands = [
        ("/code", "描述需求，开始编码"),
        ("/model", "切换 AI 模型"),
        ("/read", "读取文件内容"),
        ("/write", "写入文件"),
        ("/edit", "编辑文件（替换内容）"),
        ("/delete", "删除文件"),
        ("/files", "浏览项目文件"),
        ("/undo", "撤销上次修改 (双击Esc也可)"),
        ("/check", "运行 EmbedGuard 静态分析"),
        ("/build", "编译项目"),
        ("/flash", "烧录固件"),
        ("/monitor", "串口监视器"),
        ("/skill", "技能中心"),
        ("/rules", "查看/管理编码规则"),
        ("/config", "配置管理"),
        ("/agent", "切换/列出 Agent"),
        ("/compact", "压缩上下文"),
        ("/context", "查看上下文使用情况"),
        ("/history", "查看历史记录"),
        ("/resume", "恢复历史会话"),
        ("/clear", "清屏"),
        ("/help", "显示帮助"),
        ("/quit", "退出"),
    ]

    for cmd, desc in commands:
        table.add_row(cmd, desc)

    panel = Panel(
        table,
        border_style=StyleConstants.PANEL_BORDER,
        title=PanelTitles.HELP,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 13. 命令自动补全器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CommandCompleter:
    """prompt_toolkit 命令自动补全器

    当用户输入 / 时弹出补全列表，显示所有可用的斜杠命令。
    """

    COMMANDS = [
        "/code",
        "/model",
        "/read",
        "/write",
        "/edit",
        "/delete",
        "/files",
        "/undo",
        "/check",
        "/build",
        "/flash",
        "/monitor",
        "/skill",
        "/rules",
        "/config",
        "/agent",
        "/compact",
        "/context",
        "/history",
        "/resume",
        "/clear",
        "/help",
        "/quit",
    ]

    DESCRIPTIONS = {
        "/code": "描述需求，开始编码",
        "/model": "切换 AI 模型",
        "/read": "读取文件内容",
        "/write": "写入文件",
        "/edit": "编辑文件（替换内容）",
        "/delete": "删除文件",
        "/files": "浏览项目文件",
        "/undo": "撤销上次修改 (双击Esc也可)",
        "/check": "运行 EmbedGuard 静态分析",
        "/build": "编译项目",
        "/flash": "烧录固件",
        "/monitor": "串口监视器",
        "/skill": "技能中心",
        "/rules": "查看/管理编码规则",
        "/config": "配置管理",
        "/agent": "切换/列出 Agent",
        "/compact": "压缩上下文",
        "/context": "查看上下文使用情况",
        "/history": "查看历史记录",
        "/resume": "恢复历史会话",
        "/clear": "清屏",
        "/help": "显示帮助",
        "/quit": "退出",
    }

    # 最常用命令（输入 / 时优先显示这 6 个）
    POPULAR = ["/code", "/help", "/build", "/files", "/read", "/compact"]

    def __init__(self) -> None:
        # pattern 包含 /，让 prompt_toolkit 在输入 / 时触发补全
        # 默认 pattern 是 \w+（仅字母数字下划线），/ 不会被识别为补全词，
        # 导致输入 / 时补全菜单不弹出或只匹配到一个结果。
        # 注意：prompt_toolkit 的 pattern 参数需要编译后的 Pattern 对象，非字符串
        import re as _re
        self._completer = WordCompleter(
            self.COMMANDS,
            ignore_case=True,
            meta_dict=self.DESCRIPTIONS,
            sentence=False,
            pattern=_re.compile(r"[/\w]+"),
        )

    @property
    def completer(self) -> WordCompleter:
        """返回底层 WordCompleter 实例"""
        return self._completer


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 14. 用户输入 — OpenCode 风格上下横线包裹
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _get_history_path() -> Path:
    """获取输入历史文件路径"""
    history_dir = Path.home() / ".iron"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / "history"


class _EscState:
    """Escape 双击检测状态"""
    last_esc_time: float = 0.0
    DOUBLE_TAP_INTERVAL: float = 0.4


def get_user_input(console: Console, completer: CommandCompleter | None = None,
                   current_agent: str = "") -> str:
    """获取用户输入 — 上下横线紧贴输入行，输入 / 时内联显示命令列表

    用 prompt_toolkit Application + HSplit 布局实现：
    上横线紧贴输入行上方，下横线紧贴输入行下方。
    当用户输入以 / 开头时，在下横线下方内联显示匹配的命令列表
    （类似 Claude Code 的多命令提示，递减匹配）。
    full_screen=False 内联显示，不清屏。

    Args:
        current_agent: 当前 agent 名（显示在提示符中，便于用户感知当前 agent）

    显示效果:
        ───────────────────────────────────
          build > /c
        ───────────────────────────────────
          /code     描述需求，开始编码
          /check    运行 EmbedGuard 静态分析
          /clear    清屏
          ...
    """
    # 重置 Esc 双击状态，避免跨输入会话误触发撤销
    _EscState.last_esc_time = 0.0
    import time as _time
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, ConditionalContainer
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.dimension import D as _D
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import merge_key_bindings
    from prompt_toolkit.key_binding.bindings.basic import load_basic_bindings

    history = FileHistory(str(_get_history_path()))
    word_completer = completer.completer if completer else None

    _sep = "─" * _get_terminal_width()

    # 命令提示状态：追踪匹配列表和选中索引
    _hint_state = {"selected": 0}

    # 文本变化时重置选中索引（用户输入新字符时回到第一项）
    def _on_text_changed(_buffer):
        _hint_state["selected"] = 0

    # 先创建 buffer，命令提示 callable 需要引用它
    buf = Buffer(
        history=history,
        completer=word_completer,
        complete_while_typing=False,  # 不用自动补全菜单，改用内联提示
        on_text_changed=_on_text_changed,
    )

    def _get_matches(text: str) -> list:
        """获取当前输入对应的匹配命令列表（与提示显示一致）"""
        if not text or not text.startswith("/"):
            return []
        if text == "/":
            return list(CommandCompleter.POPULAR)
        return [c for c in CommandCompleter.COMMANDS if c.startswith(text)][:6]

    def _get_command_hints():
        """根据当前输入动态返回匹配的命令列表（内联显示）

        - 只输入 / → 显示最常用 6 个命令
        - 输入 /xxx → 前缀递减匹配，最多显示 6 个
        """
        text = buf.text
        matches = _get_matches(text)

        if not matches:
            if text and text.startswith("/"):
                return [("class:hint-empty", "  无匹配命令")]
            return []

        descs = CommandCompleter.DESCRIPTIONS
        # 渲染匹配命令，选中项高亮
        result = []
        sel = _hint_state["selected"] % len(matches) if matches else 0
        for i, cmd in enumerate(matches):
            desc = descs.get(cmd, "")
            if i == sel:
                result.append(("class:hint-cmd-selected", f"  {cmd:<12}"))
                result.append(("class:hint-desc-selected", f"{desc}"))
            else:
                result.append(("class:hint-cmd", f"  {cmd:<12}"))
                result.append(("class:hint-desc", f"{desc}"))
            result.append(("", "\n"))
        # 移除最后一个换行
        if result and result[-1] == ("", "\n"):
            result.pop()
        return result

    custom_bindings = KeyBindings()

    @custom_bindings.add(Keys.Escape, eager=True)
    def _(event):
        now = _time.time()
        if _EscState.last_esc_time and (now - _EscState.last_esc_time) < _EscState.DOUBLE_TAP_INTERVAL:
            # 双击 Esc → 撤销
            _EscState.last_esc_time = None
            event.app.exit(result="__UNDO__")
        else:
            # 单击 Esc → 仅记录时间，不清空输入
            _EscState.last_esc_time = now

    @custom_bindings.add("c-c", eager=True)
    def _(event):
        event.current_buffer.reset()
        event.app.exit(result="")

    @custom_bindings.add("c-l", eager=True)
    def _(event):
        event.app.renderer.clear()
        event.app.invalidate()

    @custom_bindings.add("c-u", eager=True)
    def _(event):
        event.current_buffer.reset(append_to_history=False)

    @custom_bindings.add("c-a", eager=True)
    def _(event):
        event.current_buffer.cursor_position = 0

    @custom_bindings.add("c-e", eager=True)
    def _(event):
        event.current_buffer.cursor_position = len(event.current_buffer.text)

    @custom_bindings.add("enter", eager=True)
    def _(event):
        text = event.current_buffer.text
        # 命令模式：以 / 开头时，回车自动补全到选中项
        if text.startswith("/"):
            matches = _get_matches(text)
            if matches:
                sel = _hint_state["selected"] % len(matches)
                # 如果当前文本不是完整命令，补全到选中项
                if text not in matches:
                    text = matches[sel]
                    # 更新缓冲区显示，让用户看到完整命令（与 Tab 行为一致）
                    event.current_buffer.text = text
                    event.current_buffer.cursor_position = len(text)
        # append_to_history 在缓冲区更新后调用，存储的是完整命令
        event.current_buffer.append_to_history()
        event.app.exit(result=text)

    @custom_bindings.add("c-d", eager=True)
    def _(event):
        event.current_buffer.reset()
        event.app.exit(result=None)

    # Tab 补全：补全到当前选中的命令；文本为空时触发 agent 切换
    @custom_bindings.add("tab", eager=True)
    def _(event):
        text = event.current_buffer.text
        matches = _get_matches(text)
        if matches:
            sel = _hint_state["selected"] % len(matches)
            event.current_buffer.text = matches[sel]
            event.current_buffer.cursor_position = len(matches[sel])
        elif not text:
            # 未输入内容时 Tab 触发 agent 切换（由 main.py 处理 /agent 命令）
            event.app.exit(result="__SWITCH_AGENT__")

    # 方向键选择命令（仅 / 命令模式拦截，普通模式交给 basic bindings 处理历史记录）
    _is_cmd_mode = Condition(lambda: buf.text.startswith("/"))

    @custom_bindings.add("down", eager=True, filter=_is_cmd_mode)
    def _(event):
        matches = _get_matches(event.current_buffer.text)
        if matches:
            _hint_state["selected"] = (_hint_state["selected"] + 1) % len(matches)

    @custom_bindings.add("up", eager=True, filter=_is_cmd_mode)
    def _(event):
        matches = _get_matches(event.current_buffer.text)
        if matches:
            _hint_state["selected"] = (_hint_state["selected"] - 1) % len(matches)

    bindings = merge_key_bindings([load_basic_bindings(), custom_bindings])

    input_control = BufferControl(buffer=buf)

    # 命令提示容器：仅在输入以 / 开头时显示
    hints_container = ConditionalContainer(
        Window(
            FormattedTextControl(_get_command_hints),
            height=_D(min=1, max=6),
        ),
        filter=Condition(lambda: buf.text.startswith("/")),
    )

    # Vim 模式检测（特性门控）
    try:
        from iron.config.features import is_feature_enabled
        _vim_enabled = is_feature_enabled("vim_mode")
    except (ImportError, RuntimeError, OSError):
        _vim_enabled = False

    # Vim 状态栏：仅 vim_mode 启用时显示
    def _get_vim_status():
        if not _vim_enabled:
            return []
        # prompt_toolkit VI 模式内部状态：app.vi_state 的 input_mode
        try:
            from prompt_toolkit.key_binding.vi_state import InputMode
            app_ref = custom_bindings.app  # 占位，实际在 app 创建后通过闭包获取
        except ImportError:
            return []
        # 通过 app 引用获取当前 vi 输入模式
        try:
            app_obj = _vim_app_ref[0]
            if app_obj is None:
                return []
            vi_state = app_obj.vi_state
            # ViState.input_mode 在不同 prompt_toolkit 版本中可能是属性或方法
            input_mode = vi_state.input_mode
            if callable(input_mode):
                input_mode = input_mode()
            if input_mode == InputMode.INSERT:
                return [("class:vim-insert", " -- INSERT -- ")]
            if input_mode == InputMode.VISUAL:
                return [("class:vim-visual", " -- VISUAL -- ")]
            return [("class:vim-normal", " -- NORMAL -- ")]
        except (AttributeError, RuntimeError):
            return [("class:vim-normal", " -- NORMAL -- ")]

    # 持有 app 引用以便状态栏 callable 获取 vi_state
    _vim_app_ref = [None]

    vim_status_container = ConditionalContainer(
        Window(FormattedTextControl(_get_vim_status), height=1),
        filter=Condition(lambda: _vim_enabled),
    )

    # 提示符：有 agent 名时显示 "  build > "，无 agent 时保持原样 "  > "
    if current_agent:
        _prompt_fragments = [
            ("class:prompt", "  "),
            ("class:agent-name", current_agent),
            ("class:prompt", " > "),
        ]
    else:
        _prompt_fragments = [("class:prompt", "  > ")]

    input_box = HSplit([
        Window(FormattedTextControl([("class:sep", _sep)]), height=1),
        VSplit([
            Window(
                FormattedTextControl(_prompt_fragments),
                dont_extend_width=True,
            ),
            Window(input_control),
        ], height=1),
        Window(FormattedTextControl([("class:sep", _sep)]), height=1),
        vim_status_container,
        hints_container,
    ])

    _style = PtStyle.from_dict({
        "prompt": "bold cyan",
        "agent-name": "fg:#ffaa00 bold",
        "sep": "fg:#888888",
        "hint-cmd": "fg:#88aabb",
        "hint-desc": "fg:#666666 italic",
        "hint-cmd-selected": "fg:#ffffff bold",
        "hint-desc-selected": "fg:#88ccff",
        "hint-empty": "fg:#888888 italic",
        "vim-normal": "fg:#88ccff bold",
        "vim-insert": "fg:#88ff88 bold",
        "vim-visual": "fg:#ffaa88 bold",
    })

    app = Application(
        layout=Layout(input_box, focused_element=input_control),
        key_bindings=bindings,
        style=_style,
        editing_mode=EditingMode.VI if _vim_enabled else EditingMode.EMACS,
        full_screen=False,
    )
    _vim_app_ref[0] = app

    try:
        result = app.run()
        if result is None:
            return "/quit"
        return result.strip()
    except EOFError:
        return "/quit"
    except KeyboardInterrupt:
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 14.5 上下键列表选择器（用于多厂商/多模型选择）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def select_with_arrows(
    options: list[tuple[object, str]],
    title: str = "",
    default_idx: int = 0,
    console: Optional[Console] = None,
) -> object:
    """用上下键从列表中选择一项

    基于 prompt_toolkit Application 实现轻量全屏选择器：
    - ↑/↓ 移动光标（当前项高亮反色）
    - Enter 确认返回选中的 value
    - Esc / Ctrl+C 取消返回 None

    Args:
        options: list of (value, description) tuples；value 是返回值，description 是显示文本
        title: 标题文本（显示在选择列表上方）
        default_idx: 默认选中项索引
        console: rich Console（非 TTY 环境下用于显示文本选择 fallback）

    Returns:
        选中的 value 或 None（取消）
    """
    if not options:
        return None

    # 边界保护：default_idx 越界时回退到 0
    if not (0 <= default_idx < len(options)):
        default_idx = 0

    # 非 TTY 环境回退到序号输入（AI 自动化测试场景）
    if not sys.stdin.isatty():
        if console is not None:
            console.print(f"\n  [bold cyan]{title}[/bold cyan]" if title else "")
            for i, (_, desc) in enumerate(options, 1):
                marker = " ◄" if i - 1 == default_idx else ""
                console.print(f"    {i}. {desc}{marker}")
            try:
                pick = input(f"  输入序号 [1-{len(options)}] (留空取消): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(options):
                    return options[idx][0]
            return None
        return None

    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window, FormattedTextControl
    from prompt_toolkit.styles import Style as PtStyle

    state = {"selected": default_idx}

    def get_text():
        lines = []
        if title:
            lines.append(("class:title", f"  {title}\n"))
            lines.append(("class:divider", "  " + "─" * 60 + "\n\n"))
        for i, (_, desc) in enumerate(options):
            if i == state["selected"]:
                lines.append(("class:selected", f"  ▶ {desc}\n"))
            else:
                lines.append(("class:unselected", f"    {desc}\n"))
        lines.append(("", "\n"))
        lines.append(("class:hint", "  ↑↓ 选择  Enter 确认  Esc 取消"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        if state["selected"] > 0:
            state["selected"] -= 1

    @kb.add("down")
    def _(event):
        if state["selected"] < len(options) - 1:
            state["selected"] += 1

    @kb.add("enter")
    def _(event):
        event.app.exit(result=options[state["selected"]][0])

    @kb.add("escape")
    def _(event):
        event.app.exit(result=None)

    layout = Layout(Window(FormattedTextControl(get_text)))

    style = PtStyle.from_dict({
        "title": "bold cyan",
        "divider": "cyan",
        "selected": "fg:#ffffff bg:#005f87 bold",
        "unselected": "fg:#cccccc",
        "hint": "fg:#888888 italic",
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        style=style,
        erase_when_done=True,
    )

    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 15. 代码差异对比视图
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_diff(
    console: Console,
    filename: str,
    old_code: str,
    new_code: str,
    language: str = "c",
) -> None:
    """显示代码修改前后的 diff 对比视图

    使用 difflib 计算差异，删除的行用红色背景，新增的行用绿色背景，
    未修改的行用 dim 样式。用 rich Panel 包裹，标题显示文件名。

    Args:
        console: rich Console 实例
        filename: 文件名（显示为面板标题）
        old_code: 修改前的代码内容
        new_code: 修改后的代码内容
        language: 编程语言（用于语法高亮，默认 c）
    """
    old_lines = old_code.splitlines() if old_code else []
    new_lines = new_code.splitlines() if new_code else []

    # 使用 difflib 计算差异
    diff = difflib.ndiff(old_lines, new_lines)

    diff_lines: list[Text] = []
    for line in diff:
        # ndiff 输出格式: 前两个字符是标记，后面是内容
        if line.startswith("- "):
            # 删除的行 — 红色背景 + - 前缀
            content = line[2:]
            text = Text()
            text.append("-", style="bold white on red")
            text.append(" ", style="on red")
            text.append(content, style="white on red")
            diff_lines.append(text)
        elif line.startswith("+ "):
            # 新增的行 — 绿色背景 + + 前缀
            content = line[2:]
            text = Text()
            text.append("+", style="bold white on green")
            text.append(" ", style="on green")
            text.append(content, style="white on green")
            diff_lines.append(text)
        elif line.startswith("  "):
            # 未修改的行 — dim 样式
            content = line[2:]
            text = Text()
            text.append(" ", style="dim")
            text.append(" ", style="dim")
            text.append(content, style="dim")
            diff_lines.append(text)
        elif line.startswith("? "):
            # 行内差异标记 — 跳过
            continue
        else:
            # 其他行 — dim 样式
            text = Text(line, style="dim")
            diff_lines.append(text)

    if not diff_lines:
        diff_lines.append(Text("  （无差异）", style="dim"))

    content = Group(*diff_lines)

    # 构建面板标题
    title = Text()
    title.append(f" {Symbols.FILE_EDIT} ", style="default")
    title.append(filename, style="bright_white")
    title.append(f"  (diff)", style="dim")

    panel = Panel(
        content,
        border_style=StyleConstants.PANEL_BORDER,
        title=title,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 17. 项目文件树
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_file_tree(
    console: Console,
    project_dir: str,
    changed_files: list[str] | None = None,
) -> None:
    """显示项目文件树

    用 rich Tree 递归显示目录结构，被修改/新建的文件用高亮标记。

    忽略 .git, __pycache__, node_modules, .iron-agent 等目录。

    Args:
        console: rich Console 实例
        project_dir: 项目根目录路径
        changed_files: 被修改/新建的文件路径列表，这些文件会被高亮标记
    """
    if changed_files is None:
        changed_files = []

    root = Path(project_dir)
    if not root.exists():
        show_error(console, f"目录不存在: {project_dir}")
        return

    if not root.is_dir():
        show_error(console, f"不是目录: {project_dir}")
        return

    # 构建变更文件集合（规范化路径用于比较）
    changed_set: set[str] = set()
    for f in changed_files:
        try:
            changed_set.add(str(Path(f).resolve()))
        except OSError:
            changed_set.add(os.path.normpath(f))

    # 构建根节点
    tree = Tree(
        f"[bold cyan]{Symbols.FOLDER} {root.name}[/bold cyan]",
        guide_style="dim cyan",
    )

    def _add_children(parent_node, path: Path, depth: int = 0, max_depth: int = 10) -> None:
        """递归添加子节点（限制最大深度，防止超深目录耗尽资源）"""
        if depth >= max_depth:
            return
        try:
            children = sorted(
                path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            parent_node.add(Text("  （无权限访问）", style="dim red"))
            return

        for child in children:
            if child.is_dir():
                # 跳过忽略目录
                if child.name in _IGNORE_DIRS:
                    continue
                # 跳过隐藏目录（以 . 开头，但允许 .. ）
                if child.name.startswith(".") and child.name not in (".", ".."):
                    continue

                child_node = parent_node.add(f"[cyan]{child.name}/[/cyan]")
                _add_children(child_node, child, depth + 1, max_depth)
            else:
                # 跳过隐藏文件
                if child.name.startswith("."):
                    continue

                # 检查是否为变更文件
                try:
                    is_changed = str(child.resolve()) in changed_set
                except OSError:
                    is_changed = os.path.normpath(str(child)) in changed_set

                if is_changed:
                    label = Text()
                    label.append(f"[bold yellow]★ {child.name}[/bold yellow]")
                    parent_node.add(label)
                else:
                    parent_node.add(child.name)

    _add_children(tree, root)

    panel = Panel(
        tree,
        border_style=StyleConstants.PANEL_BORDER,
        title=f"{Symbols.FOLDER} 文件树",
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 18. 文件内容浏览
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def show_file_content(
    console: Console,
    filepath: str,
    language: str | None = None,
) -> None:
    """浏览文件内容，带语法高亮

    读取文件内容，自动检测语言（按扩展名），用 rich Syntax 显示
    带语法高亮的文件内容，用 Panel 包裹。

    Args:
        console: rich Console 实例
        filepath: 文件路径
        language: 强制指定语言（为 None 时自动检测）
    """
    path = Path(filepath)
    if not path.exists():
        show_error(console, f"文件不存在: {filepath}")
        return

    if not path.is_file():
        show_error(console, f"不是文件: {filepath}")
        return

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        show_error(console, f"无权限读取文件: {filepath}")
        return
    except OSError as e:
        show_error(console, f"读取文件失败: {filepath}", str(e))
        return

    # 自动检测语言
    if language is None:
        language = _detect_language(path)

    syntax = Syntax(
        content,
        language,
        theme="monokai",
        line_numbers=True,
        word_wrap=True,
        padding=(1, 0),
    )

    # 构建面板标题
    title = Text()
    title.append(f" {Symbols.FILE_NEW} ", style="default")
    title.append(path.name, style="bright_white")
    title.append(f"  ({language})", style="dim")

    # 显示文件大小
    file_size = path.stat().st_size
    if file_size < 1024:
        size_str = f"{file_size} B"
    elif file_size < 1024 * 1024:
        size_str = f"{file_size / 1024:.1f} KB"
    else:
        size_str = f"{file_size / (1024 * 1024):.1f} MB"

    title.append(f"  [{size_str}]", style="dim")

    panel = Panel(
        syntax,
        border_style=StyleConstants.PANEL_BORDER,
        title=title,
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 19. 流式 Markdown 渲染器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MarkdownStreamRenderer:
    """流式 Markdown 渲染器

    逐 chunk 接收文本，实时渲染 Markdown：
    - 普通文本：按段落边界（空行）或标题（#）刷新
    - 代码块：完整接收后再用 Syntax 高亮渲染（避免抖动）
    - 不完整行：暂存到 pending，等下次 chunk 拼接后再处理

    用法:
        renderer = MarkdownStreamRenderer(console)
        for chunk in stream:
            renderer.append(chunk)
        renderer.finish()
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._buffer = ""               # 完整文本缓冲区（用于 get_full_text）
        self._pending = ""              # 上一个 chunk 末尾的不完整行
        self._in_code_block = False
        self._code_lang = ""
        self._code_buffer = ""
        self._line_buffer = ""

    def append(self, chunk: str) -> None:
        """追加文本 chunk，实时渲染"""
        if not chunk:
            return
        self._buffer += chunk
        # 拼接上一个 chunk 的残余，保证行边界判断正确
        if self._pending:
            chunk = self._pending + chunk
            self._pending = ""
        # 按行处理
        while "\n" in chunk:
            line, chunk = chunk.split("\n", 1)
            self._process_line(line + "\n")
        # 保留不完整的尾行，等下次拼接
        if chunk:
            self._pending = chunk

    def _process_line(self, line: str) -> None:
        """处理单行"""
        stripped = line.strip()

        # 检测代码块开始/结束
        if stripped.startswith("```"):
            if not self._in_code_block:
                # 代码块开始：先刷新行缓冲区，避免内容混入代码块
                self._flush_line_buffer()
                self._in_code_block = True
                self._code_lang = stripped[3:].strip()
                self._code_buffer = ""
            else:
                # 代码块结束：用 Syntax 高亮渲染整个代码块
                self._flush_code_block()
                self._in_code_block = False
                self._code_lang = ""
            return

        if self._in_code_block:
            # 代码块内容：直接累积，不渲染
            self._code_buffer += line
            return

        # 普通行：缓冲到行缓冲区
        self._line_buffer += line

        # 段落结束（空行）或标题（独占一行）→ 立即刷新
        if stripped == "" or stripped.startswith("#"):
            self._flush_line_buffer()

    def _flush_line_buffer(self) -> None:
        """渲染行缓冲区为 Markdown"""
        if not self._line_buffer.strip():
            self._line_buffer = ""
            return
        try:
            md = Markdown(self._line_buffer)
            self.console.print(md, end="")
        except Exception:
            # 渲染失败时回退到纯文本输出
            self.console.print(self._line_buffer, end="")
        self._line_buffer = ""

    def _flush_code_block(self) -> None:
        """渲染代码块（带语法高亮）"""
        if not self._code_buffer.strip():
            self._code_buffer = ""
            return
        try:
            syntax = Syntax(
                self._code_buffer.rstrip("\n"),
                self._code_lang or "text",
                theme="monokai",
                line_numbers=False,
                padding=(0, 1),
            )
            self.console.print(syntax)
        except Exception:
            # 高亮失败时回退到纯文本代码块
            self.console.print(f"```\n{self._code_buffer}\n```")
        self._code_buffer = ""

    def finish(self) -> None:
        """完成渲染，刷新剩余缓冲区"""
        # 处理最后的残余行（无尾随换行的不完整行）
        if self._pending:
            self._process_line(self._pending + "\n")
            self._pending = ""
        # 未闭合的代码块：刷新
        if self._in_code_block:
            self._flush_code_block()
            self._in_code_block = False
            self._code_lang = ""
        # 刷新剩余的行缓冲区
        self._flush_line_buffer()

    def get_full_text(self) -> str:
        """获取完整文本（用于保存到 conversation）"""
        return self._buffer


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v4.0 Track 6: Diff 预览（edit_file 前置展示）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _render_diff(console: Console, old_content: str, new_content: str,
                 file_path: str = "") -> None:
    """渲染 unified diff 到 console（带颜色 + 截断）

    用于 edit_file 执行前的 diff 预览，让用户在权限回调时看到即将发生的变更。

    Args:
        console: Rich Console 实例
        old_content: 原内容
        new_content: 新内容
        file_path: 文件路径（用于 diff 头部显示）
    """
    old_lines = old_content.splitlines(keepends=False)
    new_lines = new_content.splitlines(keepends=False)

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{file_path}" if file_path else "原文件",
        tofile=f"b/{file_path}" if file_path else "新文件",
        lineterm="",
    ))

    if not diff_lines:
        console.print(f"  {Symbols.INFO} 无变更", style="cyan")
        return

    # 截断长 diff（超过 50 行只显示前后 25 行 + 中间省略提示）
    MAX_LINES = 50
    if len(diff_lines) > MAX_LINES:
        head = diff_lines[:25]
        tail = diff_lines[-25:]
        omitted = len(diff_lines) - 50
        diff_lines = head + [f"  ... 省略 {omitted} 行 ..."] + tail

    # 渲染（带颜色）
    console.print()
    if file_path:
        console.print(f"  {Symbols.FILE_NEW} Diff: {file_path}",
                      style="bold cyan")
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            console.print(line, style="bold")
        elif line.startswith("@@"):
            console.print(line, style="cyan")
        elif line.startswith("+"):
            console.print(line, style="green")
        elif line.startswith("-"):
            console.print(line, style="red")
        else:
            console.print(line)
    console.print()

