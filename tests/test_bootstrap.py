"""P6-1: 启动管道分阶段测试

测试用例：
- test_bootstrap_result_defaults: 默认值
- test_bootstrap_config_success: 配置阶段成功
- test_bootstrap_config_failure: 配置阶段失败
- test_bootstrap_trust_success: 信任阶段成功
- test_bootstrap_trust_no_api_key: API Key 缺失警告
- test_bootstrap_run_success: 运行阶段成功
- test_bootstrap_run_theme_failure: 主题加载失败降级
- test_bootstrap_full_success: 完整启动成功
- test_bootstrap_phase_order: 阶段顺序
- test_bootstrap_errors_collected: 错误收集

运行方式: pytest tests/test_bootstrap.py -v
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from rich.console import Console

from iron.cli.bootstrap import Bootstrap, BootstrapResult


# ── 测试夹具 ──────────────────────────────────────────────────

@pytest.fixture
def quiet_console():
    """静默 Console，避免测试输出污染"""
    return Console(quiet=True, width=80)


@pytest.fixture
def mock_config():
    """模拟配置对象 — 用于信任/运行阶段测试"""
    config = MagicMock()
    config.llm.backend = "echo"
    config.llm.model = "gpt-4o"
    config.llm.api_key = "test-key"
    config.llm.base_url = "https://api.openai.com/v1"
    config.llm.request_timeout = 120
    config.llm.available_models = []
    config.project.mcu = "stm32f407"
    config.project.build_system = "platformio"
    config.theme = "default"
    config.mcp = {}
    config.verbose = False
    config.providers = []
    return config


# ── 1. BootstrapResult 默认值测试 ─────────────────────────────

def test_bootstrap_result_defaults():
    """测试 BootstrapResult 默认值"""
    result = BootstrapResult()
    assert result.success is False
    assert result.config is None
    assert result.llm is None
    assert result.prompt_builder is None
    assert result.skills is None
    assert result.errors == []
    assert result.warnings == []
    assert result.phases_executed == []
    # 验证默认列表字段独立（不共享引用）
    r1 = BootstrapResult()
    r2 = BootstrapResult()
    r1.errors.append("test")
    assert r2.errors == [], "不同实例的 errors 不应共享引用"


# ── 2. 配置阶段成功测试 ───────────────────────────────────────

def test_bootstrap_config_success(quiet_console, tmp_path):
    """测试配置阶段成功加载"""
    bootstrap = Bootstrap(quiet_console)
    config = bootstrap._phase_config(tmp_path, None, None, None, False)

    assert config is not None
    assert "config" in bootstrap._phases_executed
    # 验证 verbose 传递
    assert config.verbose is False


def test_bootstrap_config_overrides(quiet_console, tmp_path):
    """测试 CLI 参数覆盖配置"""
    bootstrap = Bootstrap(quiet_console)
    config = bootstrap._phase_config(
        tmp_path, mcu="esp32", model="gpt-4o-mini", backend="echo", verbose=True
    )
    assert config is not None
    assert config.project.mcu == "esp32"
    assert config.llm.model == "gpt-4o-mini"
    assert config.llm.backend == "echo"
    assert config.verbose is True


# ── 3. 配置阶段失败测试 ──────────────────────────────────────

def test_bootstrap_config_failure(quiet_console, tmp_path):
    """测试配置阶段失败 — IronConfig.load 抛异常"""
    bootstrap = Bootstrap(quiet_console)
    with patch("iron.config.settings.IronConfig.load", side_effect=Exception("配置文件损坏")):
        config = bootstrap._phase_config(tmp_path, None, None, None, False)

    assert config is None
    assert len(bootstrap._errors) == 1
    assert "配置加载失败" in bootstrap._errors[0]
    assert "配置文件损坏" in bootstrap._errors[0]


# ── 4. 信任阶段成功测试 ──────────────────────────────────────

def test_bootstrap_trust_success(quiet_console, mock_config):
    """测试信任阶段成功创建 LLM 后端"""
    bootstrap = Bootstrap(quiet_console)
    llm = bootstrap._phase_trust(mock_config)

    assert llm is not None
    assert "trust" in bootstrap._phases_executed
    # 有 API Key 时不应有警告
    assert not any("API Key" in w for w in bootstrap._warnings)


# ── 5. API Key 缺失警告测试 ───────────────────────────────────

def test_bootstrap_trust_no_api_key(quiet_console, mock_config):
    """测试 API Key 缺失时产生警告（不阻塞）"""
    bootstrap = Bootstrap(quiet_console)
    mock_config.llm.api_key = ""  # 模拟 API Key 未设置

    llm = bootstrap._phase_trust(mock_config)

    # 后端仍应创建成功（echo 后端不需要 key）
    assert llm is not None
    # 应有 API Key 未设置警告
    assert any("API Key" in w for w in bootstrap._warnings)


# ── 6. 运行阶段成功测试 ──────────────────────────────────────

def test_bootstrap_run_success(quiet_console, mock_config, tmp_path):
    """测试运行阶段成功初始化组件"""
    bootstrap = Bootstrap(quiet_console)
    prompt_builder, skills, _ = bootstrap._phase_run(mock_config, tmp_path)

    assert prompt_builder is not None
    assert skills is not None
    assert "run" in bootstrap._phases_executed
    # 运行阶段成功不应有错误
    assert len(bootstrap._errors) == 0


# ── 7. 主题加载失败降级测试 ───────────────────────────────────

def test_bootstrap_run_theme_failure(quiet_console, mock_config, tmp_path):
    """测试主题加载失败时优雅降级（不阻塞，仅警告）"""
    bootstrap = Bootstrap(quiet_console)
    with patch("iron.cli.theme.set_theme", side_effect=Exception("主题文件损坏")):
        prompt_builder, skills, _ = bootstrap._phase_run(mock_config, tmp_path)

    # 主题失败不阻塞，prompt_builder 和 skills 仍应创建成功
    assert prompt_builder is not None
    assert skills is not None
    # 应有主题加载失败警告
    assert any("主题加载失败" in w for w in bootstrap._warnings)
    # 不应有错误（降级处理）
    assert len(bootstrap._errors) == 0


# ── 8. 完整启动成功测试 ──────────────────────────────────────

def test_bootstrap_full_success(quiet_console, tmp_path):
    """测试完整 3 阶段启动成功"""
    bootstrap = Bootstrap(quiet_console)
    result = bootstrap.run(tmp_path, backend="echo", verbose=False)

    assert result.success is True
    assert result.config is not None
    assert result.llm is not None
    assert result.prompt_builder is not None
    assert result.skills is not None
    # 三个阶段都应执行
    assert result.phases_executed == ["config", "trust", "run"]
    # 不应有错误
    assert result.errors == []


# ── 9. 阶段顺序测试 ──────────────────────────────────────────

def test_bootstrap_phase_order(quiet_console, tmp_path):
    """测试阶段执行顺序为 config → trust → run"""
    bootstrap = Bootstrap(quiet_console)
    result = bootstrap.run(tmp_path, backend="echo")

    assert result.phases_executed == ["config", "trust", "run"]
    # 验证顺序：config 必须在 trust 之前，trust 必须在 run 之前
    phases = result.phases_executed
    assert phases.index("config") < phases.index("trust")
    assert phases.index("trust") < phases.index("run")


def test_bootstrap_phase_order_on_config_failure(quiet_console, tmp_path):
    """测试配置失败时只执行 config 阶段"""
    bootstrap = Bootstrap(quiet_console)
    with patch("iron.config.settings.IronConfig.load", side_effect=Exception("失败")):
        result = bootstrap.run(tmp_path)

    assert result.success is False
    # 配置失败后不应执行 trust 和 run 阶段
    assert result.phases_executed == ["config"]


# ── 10. 错误收集测试 ──────────────────────────────────────────

def test_bootstrap_errors_collected(quiet_console, tmp_path):
    """测试错误被正确收集到 result.errors"""
    bootstrap = Bootstrap(quiet_console)
    with patch("iron.config.settings.IronConfig.load", side_effect=Exception("测试错误")):
        result = bootstrap.run(tmp_path)

    assert result.success is False
    assert len(result.errors) >= 1
    # 错误信息应包含失败原因
    assert any("配置加载失败" in e for e in result.errors)
    assert any("测试错误" in e for e in result.errors)


def test_bootstrap_warnings_isolated_from_errors(quiet_console, mock_config, tmp_path):
    """测试警告与错误隔离 — 主题失败是警告不是错误"""
    bootstrap = Bootstrap(quiet_console)
    with patch("iron.cli.theme.set_theme", side_effect=RuntimeError("主题问题")):
        result = bootstrap._phase_run(mock_config, tmp_path)

    # 警告列表中应有主题问题
    assert any("主题加载失败" in w for w in bootstrap._warnings)
    # 错误列表应为空（主题失败降级，不算错误）
    assert bootstrap._errors == []
