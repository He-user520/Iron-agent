"""P6-2: 特性门控测试 — 运行时特性开关

测试用例：
- test_default_features: 默认特性值
- test_is_enabled: 查询启用
- test_enable: 启用特性
- test_disable: 禁用特性
- test_set: 设置特性
- test_unknown_feature: 未知特性返回 False
- test_load_user_overrides: 加载用户覆盖
- test_save_and_reload: 保存和重新加载
- test_list_all: 列出所有
- test_list_enabled: 列出已启用
- test_list_disabled: 列出已禁用
- test_reset_to_defaults: 重置默认值
- test_global_singleton: 全局单例
- test_is_feature_enabled: 快捷查询

运行方式: pytest tests/test_features.py -v
"""
import pytest
from pathlib import Path

from iron.config.features import (
    DEFAULT_FEATURES,
    FeatureFlags,
    get_feature_flags,
    reset_global_flags,
    is_feature_enabled,
)


# ── 测试夹具 ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_global_singleton():
    """每个测试前后重置全局单例，避免测试间状态污染

    autouse=True 自动应用到本模块所有测试，确保全局单例隔离。
    """
    reset_global_flags()
    yield
    reset_global_flags()


@pytest.fixture
def tmp_features_file(tmp_path):
    """临时特性配置文件路径（每个测试独立，避免互相干扰）"""
    return tmp_path / "features.yml"


@pytest.fixture
def flags(tmp_features_file):
    """使用临时配置文件的 FeatureFlags 实例（文件不存在，使用默认值）"""
    return FeatureFlags(config_path=tmp_features_file)


# ── 1. 默认特性值测试 ─────────────────────────────────────────

def test_default_features(flags):
    """测试默认特性值正确加载"""
    all_flags = flags.list_all()
    # 验证所有默认特性都已加载
    assert len(all_flags) == len(DEFAULT_FEATURES)
    # 验证关键默认值
    assert flags.is_enabled("prompt_caching") is True
    assert flags.is_enabled("stop_hooks") is True
    assert flags.is_enabled("permission_rules") is True
    assert flags.is_enabled("pubsub") is True
    assert flags.is_enabled("bootstrap_pipeline") is True
    # 验证可选功能默认关闭
    assert flags.is_enabled("lsp_tools") is False
    assert flags.is_enabled("vim_mode") is False
    assert flags.is_enabled("search_mode") is False


# ── 2. 查询启用测试 ───────────────────────────────────────────

def test_is_enabled(flags):
    """测试 is_enabled 查询已启用和已禁用的特性"""
    # 已启用的特性返回 True
    assert flags.is_enabled("prompt_caching") is True
    assert flags.is_enabled("stop_hooks") is True
    assert flags.is_enabled("tool_search") is True
    # 已禁用的特性返回 False
    assert flags.is_enabled("lsp_tools") is False
    assert flags.is_enabled("vim_mode") is False


# ── 3. 启用特性测试 ──────────────────────────────────────────

def test_enable(flags):
    """测试 enable 方法启用特性"""
    # 启用已关闭的特性
    assert flags.is_enabled("vim_mode") is False
    assert flags.enable("vim_mode") is True
    assert flags.is_enabled("vim_mode") is True
    # 启用已开启的特性（幂等）
    assert flags.enable("prompt_caching") is True
    assert flags.is_enabled("prompt_caching") is True


# ── 4. 禁用特性测试 ──────────────────────────────────────────

def test_disable(flags):
    """测试 disable 方法禁用特性"""
    # 禁用已开启的特性
    assert flags.is_enabled("prompt_caching") is True
    assert flags.disable("prompt_caching") is True
    assert flags.is_enabled("prompt_caching") is False
    # 禁用已关闭的特性（幂等）
    assert flags.disable("lsp_tools") is True
    assert flags.is_enabled("lsp_tools") is False


# ── 5. 设置特性测试 ───────────────────────────────────────────

def test_set(flags):
    """测试 set 方法设置特性状态"""
    # 设置为 True
    assert flags.set("vim_mode", True) is True
    assert flags.is_enabled("vim_mode") is True
    # 设置为 False
    assert flags.set("prompt_caching", False) is True
    assert flags.is_enabled("prompt_caching") is False
    # set 接受非 bool 值会被转为 bool
    assert flags.set("lsp_tools", 1) is True
    assert flags.is_enabled("lsp_tools") is True
    assert flags.set("lsp_tools", 0) is True
    assert flags.is_enabled("lsp_tools") is False


# ── 6. 未知特性测试 ──────────────────────────────────────────

def test_unknown_feature(flags):
    """测试未知特性名在所有操作中都返回 False"""
    # is_enabled 对未知特性返回 False
    assert flags.is_enabled("nonexistent_feature") is False
    # enable 对未知特性返回 False
    assert flags.enable("nonexistent_feature") is False
    # disable 对未知特性返回 False
    assert flags.disable("nonexistent_feature") is False
    # set 对未知特性返回 False
    assert flags.set("nonexistent_feature", True) is False


# ── 7. 加载用户覆盖测试 ───────────────────────────────────────

def test_load_user_overrides(tmp_features_file):
    """测试从 YAML 文件加载用户覆盖值"""
    # 写入用户覆盖配置
    tmp_features_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_features_file.write_text(
        "vim_mode: true\n"
        "lsp_tools: true\n"
        "prompt_caching: false\n",
        encoding="utf-8",
    )
    # 创建实例应加载覆盖值
    flags = FeatureFlags(config_path=tmp_features_file)
    # 验证覆盖生效
    assert flags.is_enabled("vim_mode") is True
    assert flags.is_enabled("lsp_tools") is True
    assert flags.is_enabled("prompt_caching") is False
    # 验证未覆盖的特性保持默认值
    assert flags.is_enabled("stop_hooks") is True


# ── 8. 保存和重新加载测试 ────────────────────────────────────

def test_save_and_reload(tmp_features_file):
    """测试 save 后重新加载能恢复状态"""
    # 创建实例并修改状态
    flags = FeatureFlags(config_path=tmp_features_file)
    flags.enable("vim_mode")
    flags.disable("prompt_caching")
    # 保存到文件
    flags.save()
    # 验证文件已创建
    assert tmp_features_file.exists()
    # 重新加载
    reloaded = FeatureFlags(config_path=tmp_features_file)
    # 验证状态恢复
    assert reloaded.is_enabled("vim_mode") is True
    assert reloaded.is_enabled("prompt_caching") is False
    # 验证其他特性保持默认
    assert reloaded.is_enabled("stop_hooks") is True


# ── 9. 列出所有测试 ──────────────────────────────────────────

def test_list_all(flags):
    """测试 list_all 返回所有特性及状态"""
    all_flags = flags.list_all()
    # 验证返回的是所有默认特性
    assert all_flags == DEFAULT_FEATURES
    # 验证返回的是副本（修改不影响内部状态）
    all_flags["vim_mode"] = True
    assert flags.is_enabled("vim_mode") is False  # 内部状态未变


# ── 10. 列出已启用测试 ───────────────────────────────────────

def test_list_enabled(flags):
    """测试 list_enabled 返回已启用的特性名列表"""
    enabled = flags.list_enabled()
    # 验证默认启用的特性在列表中
    assert "prompt_caching" in enabled
    assert "stop_hooks" in enabled
    assert "pubsub" in enabled
    # 验证默认禁用的特性不在列表中
    assert "vim_mode" not in enabled
    assert "lsp_tools" not in enabled
    # 验证数量正确
    expected_count = sum(1 for v in DEFAULT_FEATURES.values() if v)
    assert len(enabled) == expected_count


# ── 11. 列出已禁用测试 ───────────────────────────────────────

def test_list_disabled(flags):
    """测试 list_disabled 返回已禁用的特性名列表"""
    disabled = flags.list_disabled()
    # 验证默认禁用的特性在列表中
    assert "vim_mode" in disabled
    assert "lsp_tools" in disabled
    assert "search_mode" in disabled
    # 验证默认启用的特性不在列表中
    assert "prompt_caching" not in disabled
    assert "stop_hooks" not in disabled
    # 验证数量正确
    expected_count = sum(1 for v in DEFAULT_FEATURES.values() if not v)
    assert len(disabled) == expected_count


# ── 12. 重置默认值测试 ───────────────────────────────────────

def test_reset_to_defaults(flags):
    """测试 reset_to_defaults 恢复所有特性到默认值"""
    # 修改多个特性状态
    flags.enable("vim_mode")
    flags.disable("prompt_caching")
    flags.disable("stop_hooks")
    # 验证修改生效
    assert flags.is_enabled("vim_mode") is True
    assert flags.is_enabled("prompt_caching") is False
    assert flags.is_enabled("stop_hooks") is False
    # 重置
    flags.reset_to_defaults()
    # 验证恢复默认值
    assert flags.is_enabled("vim_mode") is False
    assert flags.is_enabled("prompt_caching") is True
    assert flags.is_enabled("stop_hooks") is True


# ── 13. 全局单例测试 ─────────────────────────────────────────

def test_global_singleton():
    """测试 get_feature_flags 返回全局单例"""
    # 重置后首次获取
    reset_global_flags()
    flags1 = get_feature_flags()
    flags2 = get_feature_flags()
    # 验证同一实例
    assert flags1 is flags2
    # 验证修改单例会影响后续查询
    flags1.enable("vim_mode")
    assert flags2.is_enabled("vim_mode") is True
    # 重置后获取新实例
    reset_global_flags()
    flags3 = get_feature_flags()
    assert flags3 is not flags1
    # 新实例使用默认值
    assert flags3.is_enabled("vim_mode") is False


# ── 14. 快捷查询测试 ─────────────────────────────────────────

def test_is_feature_enabled():
    """测试 is_feature_enabled 快捷查询函数"""
    reset_global_flags()
    # 验证默认值查询
    assert is_feature_enabled("prompt_caching") is True
    assert is_feature_enabled("stop_hooks") is True
    assert is_feature_enabled("vim_mode") is False
    # 验证未知特性返回 False
    assert is_feature_enabled("nonexistent") is False
    # 验证与 get_feature_flags().is_enabled() 一致
    flags = get_feature_flags()
    flags.disable("prompt_caching")
    assert is_feature_enabled("prompt_caching") is False
