"""默认主题 — 保持与原 Colors 类一致的配色"""
# 与原 iron/cli/theme.py 中 Colors 类的硬编码值保持一致，
# 切换到 default 主题时视觉无变化，确保向后兼容。

DEFAULT_THEME = {
    "primary": "cyan",
    "secondary": "blue",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "muted": "dim",
    "accent": "magenta",
    "code": "bright_white",
    "heading": "bold cyan",
    "link": "underline blue",
    "panel_border": "cyan",
    "separator": "dim cyan",
    "input_prefix": "bold cyan",
    "prompt": "bold cyan",
    # prompt_toolkit 补全菜单
    "completion_bg": "#1e1e1e",
    "completion_fg": "#ffffff",
    "completion_current_bg": "#005f87",
    "completion_current_fg": "#ffffff",
}
