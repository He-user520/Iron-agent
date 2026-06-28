"""主题系统 — 可切换配色方案

提供三套内置主题：default / catppuccin / dracula。
通过 get_theme(name) 获取配色字典，list_themes() 列出所有可用主题名。

用法:
    from iron.cli.themes import get_theme, list_themes
    theme = get_theme("catppuccin")
    primary_color = theme["primary"]

切换运行时主题请使用 iron.cli.theme.set_theme(name)。
"""
from iron.cli.themes.default import DEFAULT_THEME
from iron.cli.themes.catppuccin import CATPPUCCIN_THEME
from iron.cli.themes.dracula import DRACULA_THEME

# 所有可用主题的注册表（名称 → 配色字典）
THEMES = {
    "default": DEFAULT_THEME,
    "catppuccin": CATPPUCCIN_THEME,
    "dracula": DRACULA_THEME,
}


def get_theme(name: str = "default") -> dict:
    """获取主题配色字典

    Args:
        name: 主题名称（default / catppuccin / dracula）

    Returns:
        主题配色字典；若 name 不存在则返回默认主题。
    """
    return THEMES.get(name, DEFAULT_THEME)


def list_themes() -> list[str]:
    """列出所有可用主题名"""
    return list(THEMES.keys())
