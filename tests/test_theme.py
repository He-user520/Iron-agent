"""P5-2: 主题系统测试 — 可切换配色方案

覆盖主题注册、获取、切换、Colors 动态代理、配置持久化、UI 接入、字段完整性。

运行方式: pytest tests/test_theme.py -v
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from iron.cli.themes import (
    DEFAULT_THEME,
    CATPPUCCIN_THEME,
    DRACULA_THEME,
    THEMES,
    get_theme,
    list_themes,
)
from iron.cli import theme as theme_module
from iron.cli.theme import Colors, get_current_theme, set_theme


# ── 公共 fixture：每个测试后恢复默认主题，避免污染其他测试 ──


@pytest.fixture(autouse=True)
def _reset_theme():
    """每个测试运行前确保主题为 default；测试后也恢复为 default"""
    set_theme("default")
    yield
    set_theme("default")


# ── 1. 默认主题 ──────────────────────────────────────────────


def test_get_theme_default():
    """test_get_theme_default: 默认主题返回 DEFAULT_THEME"""
    theme = get_theme("default")
    assert theme is DEFAULT_THEME
    # 关键字段存在且值正确
    assert theme["primary"] == "cyan"
    assert theme["error"] == "red"


# ── 2. Catppuccin 主题 ──────────────────────────────────────


def test_get_theme_catppuccin():
    """test_get_theme_catppuccin: Catppuccin 主题返回正确字典"""
    theme = get_theme("catppuccin")
    assert theme is CATPPUCCIN_THEME
    # Catppuccin 用十六进制色值
    assert theme["primary"] == "#89b4fa"
    assert theme["error"] == "#f38ba8"
    assert theme["completion_bg"] == "#1e1e2e"


# ── 3. Dracula 主题 ──────────────────────────────────────────


def test_get_theme_dracula():
    """test_get_theme_dracula: Dracula 主题返回正确字典"""
    theme = get_theme("dracula")
    assert theme is DRACULA_THEME
    assert theme["primary"] == "#bd93f9"
    assert theme["error"] == "#ff5555"
    assert theme["completion_bg"] == "#282a36"


# ── 4. 无效主题回退 ──────────────────────────────────────────


def test_get_theme_invalid():
    """test_get_theme_invalid: 无效主题名返回默认主题"""
    theme = get_theme("nonexistent-theme")
    assert theme is DEFAULT_THEME
    # get_theme 默认参数也返回默认主题
    assert get_theme() is DEFAULT_THEME


# ── 5. 列出主题 ──────────────────────────────────────────────


def test_list_themes():
    """test_list_themes: 列出所有内置主题名"""
    names = list_themes()
    assert "default" in names
    assert "catppuccin" in names
    assert "dracula" in names
    # 恰好 3 个内置主题
    assert len(names) == 3
    # 与 THEMES 注册表键一致
    assert set(names) == set(THEMES.keys())


# ── 6. 切换主题 ──────────────────────────────────────────────


def test_set_theme():
    """test_set_theme: set_theme 切换当前主题"""
    # 初始为 default
    assert get_current_theme() is DEFAULT_THEME
    # 切换到 catppuccin
    set_theme("catppuccin")
    assert get_current_theme() is CATPPUCCIN_THEME
    # 切换到 dracula
    set_theme("dracula")
    assert get_current_theme() is DRACULA_THEME
    # 切换回 default
    set_theme("default")
    assert get_current_theme() is DEFAULT_THEME


def test_set_theme_invalid_falls_back():
    """test_set_theme: 无效主题名回退到 default（不抛异常）"""
    set_theme("totally-bogus")
    assert get_current_theme() is DEFAULT_THEME


# ── 7. Colors 动态读取 ──────────────────────────────────────


def test_colors_dynamic():
    """test_colors_dynamic: Colors 代理动态读取当前主题"""
    # 默认主题下，Colors.PRIMARY 与 DEFAULT_THEME["primary"] 一致
    assert Colors.PRIMARY == DEFAULT_THEME["primary"]
    assert Colors.ERROR == DEFAULT_THEME["error"]
    assert Colors.PANEL_BORDER == DEFAULT_THEME["panel_border"]

    # 切换到 catppuccin 后，Colors 动态反映新主题（无需重新 import）
    set_theme("catppuccin")
    assert Colors.PRIMARY == CATPPUCCIN_THEME["primary"]
    assert Colors.ERROR == CATPPUCCIN_THEME["error"]
    assert Colors.PANEL_BORDER == CATPPUCCIN_THEME["panel_border"]

    # 切换到 dracula
    set_theme("dracula")
    assert Colors.PRIMARY == DRACULA_THEME["primary"]
    assert Colors.PROMPT == DRACULA_THEME["prompt"]


def test_colors_unknown_attr_returns_white():
    """Colors 代理对未知属性返回 'white' 兜底，不抛 AttributeError"""
    assert Colors.NONEXISTENT_KEY == "white"


# ── 8. 主题从配置加载（持久化） ──────────────────────────────


def test_theme_persistence(tmp_path: Path):
    """test_theme_persistence: 主题从 YAML 配置加载并 round-trip 保存"""
    from iron.config.settings import IronConfig

    # 构造一个临时全局配置文件，写入 theme: catppuccin
    config_dir = tmp_path / ".iron"
    config_dir.mkdir()
    config_file = config_dir / "config.yml"
    config_file.write_text("theme: dracula\n", encoding="utf-8")

    # 临时替换 DEFAULT_CONFIG_DIR，让 IronConfig.load 读到我们的临时文件
    original_default = theme_module  # not used, just for clarity
    import iron.config.settings as settings_module
    original_dir = settings_module.DEFAULT_CONFIG_DIR
    try:
        settings_module.DEFAULT_CONFIG_DIR = config_dir
        loaded = IronConfig.load()
        assert loaded.theme == "dracula"

        # 保存到另一个临时文件，验证 theme 字段被写入 YAML
        out_file = tmp_path / "out.yml"
        loaded.save(out_file)
        content = out_file.read_text(encoding="utf-8")
        assert "theme: dracula" in content
    finally:
        settings_module.DEFAULT_CONFIG_DIR = original_dir


def test_theme_invalid_value_ignored(tmp_path: Path):
    """配置文件中非法 theme 值被忽略，保留默认 'default'"""
    from iron.config.settings import IronConfig

    config_dir = tmp_path / ".iron"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("theme: not-a-real-theme\n", encoding="utf-8")

    import iron.config.settings as settings_module
    original_dir = settings_module.DEFAULT_CONFIG_DIR
    try:
        settings_module.DEFAULT_CONFIG_DIR = config_dir
        loaded = IronConfig.load()
        assert loaded.theme == "default"
    finally:
        settings_module.DEFAULT_CONFIG_DIR = original_dir


# ── 9. UI 使用主题颜色（mock 检查） ──────────────────────────


def test_ui_uses_theme():
    """test_ui_uses_theme: UI 模块通过 Colors 代理读取主题颜色

    验证两点：
    1. iron.cli.ui 模块确实从 iron.cli.theme 导入了 Colors（接入主题系统）；
    2. 切换主题后，ui 模块看到的 Colors.PRIMARY 等属性随之变化。
    """
    import iron.cli.ui as ui_module

    # ui 模块导入了 Colors（来自主题系统）
    assert hasattr(ui_module, "Colors")
    # 该 Colors 就是 theme 模块导出的代理实例
    assert ui_module.Colors is theme_module.Colors

    # 切换主题后，ui 看到的颜色值动态变化
    set_theme("default")
    assert ui_module.Colors.PRIMARY == DEFAULT_THEME["primary"]

    set_theme("dracula")
    assert ui_module.Colors.PRIMARY == DRACULA_THEME["primary"]
    assert ui_module.Colors.PANEL_BORDER == DRACULA_THEME["panel_border"]


# ── 10. 所有主题有必需字段 ──────────────────────────────────


def test_all_themes_have_required_keys():
    """test_all_themes_have_required_keys: 每个内置主题都包含必需字段"""
    required_keys = {
        "primary",
        "secondary",
        "success",
        "warning",
        "error",
        "muted",
        "accent",
        "code",
        "heading",
        "link",
        "panel_border",
        "separator",
        "input_prefix",
        "prompt",
        "completion_bg",
        "completion_fg",
        "completion_current_bg",
        "completion_current_fg",
    }
    for name, theme in THEMES.items():
        missing = required_keys - set(theme.keys())
        assert not missing, f"主题 '{name}' 缺少字段: {missing}"
        # 所有值都是字符串（rich / prompt_toolkit 样式字符串）
        for key, val in theme.items():
            assert isinstance(val, str), f"主题 '{name}' 的 {key} 值不是字符串: {type(val)}"
