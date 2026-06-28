"""Iron CLI 主题 — 颜色、符号、样式常量

支持运行时切换主题：通过 set_theme(name) 切换。
Colors 类保持向后兼容（动态读取当前主题，访问 Colors.PRIMARY 等仍可用）。
"""
from iron.cli.themes import get_theme, list_themes

# 当前主题（模块级单例） — 默认主题
_current_theme = get_theme("default")


def set_theme(name: str) -> None:
    """切换当前主题

    Args:
        name: 主题名称（default / catppuccin / dracula）；无效名称回退到 default。
    """
    global _current_theme
    from iron.cli.themes import THEMES
    _current_theme = THEMES.get(name, THEMES["default"])


def get_current_theme() -> dict:
    """获取当前主题配色字典"""
    return _current_theme


class _ColorsProxy:
    """Colors 代理 — 动态读取当前主题

    保持向后兼容：现有代码访问 Colors.PRIMARY / Colors.ERROR 等仍可工作，
    实际值随 _current_theme 切换而变化（大写属性名 → 小写主题键名）。
    """

    def __getattr__(self, name: str) -> str:
        key = name.lower()
        return _current_theme.get(key, "white")


# 模块级单例 — 替代原 Colors 类
Colors = _ColorsProxy()


class Symbols:
    """统一符号集"""
    # 状态
    CHECK = "✓"
    CROSS = "✗"
    WARN = "⚠"
    INFO = "ℹ"
    ARROW = "→"
    BULLET = "•"
    DIAMOND = "◆"

    # 流程
    THINKING = "⏳"
    DONE = "✅"
    WRENCH = "🔧"
    HAMMER = "🔨"
    MAGNIFY = "🔍"
    ROCKET = "🚀"
    BOLT = "⚡"
    SERIAL = "📡"
    BRAIN = "🧠"
    SHIELD = "🛡️"

    # 文件
    FILE_NEW = "📄"
    FILE_EDIT = "✏️"
    FOLDER = "📁"

    # 树形
    TREE_BRANCH = "├─"
    TREE_LAST = "└─"
    TREE_PIPE = "│"
    TREE_SPACE = "  "

    # UI
    PROMPT = ">"
    DIVIDER = "─"
    BOX_TL = "┌"
    BOX_TR = "┐"
    BOX_BL = "└"
    BOX_BR = "┘"
    BOX_H = "─"
    BOX_V = "│"


class PanelTitles:
    """面板标题"""
    WELCOME = "Iron v{version}"
    UNDERSTAND = "理解"
    PLAN = "计划"
    CLARIFY = "提问"
    IMPLEMENT = "编码"
    REVIEW = "审查"
    SUMMARY = "完成"
    STATUS = "环境"
    SKILL = "技能"
    ERROR = "错误"
    RULES = "规则"
    HELP = "帮助"


# 嵌入式铁律描述（用于 UI 展示）
IRON_RULE_NAMES = [
    "禁止动态内存分配",
    "禁止递归调用",
    "MMIO 必须 volatile",
    "ISR 禁止阻塞操作",
    "优先位操作",
    "避免浮点运算",
    "数组必须有边界",
    "返回值必须检查",
    "共享变量需临界区",
    "禁止 goto/setjmp",
    "禁止标准库 I/O",
]
