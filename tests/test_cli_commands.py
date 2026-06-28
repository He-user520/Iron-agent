"""P5-3: 斜杠命令分组测试

测试用例：
- test_command_groups: 命令分组正确
- test_file_commands_dispatch: 文件命令分发
- test_build_commands_dispatch: 构建命令分发
- test_session_commands_dispatch: 会话命令分发
- test_system_commands_dispatch: 系统命令分发
- test_unknown_command: 未知命令返回 False
- test_help_command: /help 命令
- test_quit_command: /quit 命令
- test_clear_command: /clear 命令
- test_command_ctx_keys: 上下文包含必需字段
"""
import pytest
from unittest.mock import MagicMock, patch
from rich.console import Console

from iron.cli.commands import (
    COMMAND_GROUPS,
    handle_file_commands,
    handle_build_commands,
    handle_session_commands,
    handle_system_commands,
)


# ── 测试夹具 ──────────────────────────────────────────────────

@pytest.fixture
def mock_ctx(tmp_path):
    """创建模拟的命令分发上下文

    包含所有 handler 需要的字段，使用 MagicMock 避免真实副作用。
    """
    console = Console(quiet=True, width=80)
    config = MagicMock()
    config.active_provider = "test"
    config.llm.backend = "openai"
    config.llm.model = "gpt-4o"
    config.llm.api_key = "test-key"
    config.llm.base_url = "https://api.openai.com/v1"
    config.llm.available_models = []
    config.providers = []
    config.project.mcu = "stm32f407"
    config.project.build_system = "platformio"
    config.max_fix_rounds = 3

    skills = MagicMock()
    skills.list_all.return_value = []

    return {
        "console": console,
        "config": config,
        "project_root": tmp_path,
        "llm": MagicMock(),
        "prompt_builder": MagicMock(),
        "skills": skills,
        "last_engine": None,
        "session": MagicMock(),
        "loop": MagicMock(),
        "total_rules": 10,
        "should_quit": False,
    }


# ── 1. 命令分组测试 ───────────────────────────────────────────

def test_command_groups():
    """测试命令分组正确"""
    # 验证四个分组都存在
    assert set(COMMAND_GROUPS.keys()) == {"file", "build", "session", "system"}

    # 验证文件命令分组
    assert set(COMMAND_GROUPS["file"]) == {
        "/read", "/write", "/edit", "/delete", "/files", "/undo",
    }

    # 验证构建命令分组
    assert set(COMMAND_GROUPS["build"]) == {
        "/code", "/check", "/build", "/flash", "/monitor", "/verify", "/explore",
    }

    # 验证会话命令分组
    assert set(COMMAND_GROUPS["session"]) == {
        "/history", "/resume", "/compact", "/context", "/clear",
    }

    # 验证系统命令分组
    assert set(COMMAND_GROUPS["system"]) == {
        "/model", "/skill", "/rules", "/config", "/agent", "/help", "/quit",
    }

    # 所有命令不应重复
    all_cmds = []
    for cmds in COMMAND_GROUPS.values():
        all_cmds.extend(cmds)
    assert len(all_cmds) == len(set(all_cmds)), "命令分组中有重复命令"


# ── 2. 文件命令分发测试 ───────────────────────────────────────

def test_file_commands_dispatch(mock_ctx):
    """测试文件命令分发"""
    # 文件命令应返回 True
    for cmd in COMMAND_GROUPS["file"]:
        assert handle_file_commands(cmd, "", mock_ctx) is True, f"{cmd} 应返回 True"

    # 非文件命令应返回 False
    assert handle_file_commands("/code", "", mock_ctx) is False
    assert handle_file_commands("/model", "", mock_ctx) is False
    assert handle_file_commands("/help", "", mock_ctx) is False
    assert handle_file_commands("/build", "", mock_ctx) is False


# ── 3. 构建命令分发测试 ───────────────────────────────────────

@patch('iron.cli.commands.build_cmds._ThinkingSpinner')
@patch('iron.cli.commands.build_cmds._run_agent', return_value=MagicMock())
@patch('iron.integrations.embedguard_bridge.analyze_paths', return_value=[])
@patch('iron.integrations.embedforge_bridge.compile_project',
       return_value={"success": False, "tool": "test", "output": ""})
@patch('iron.integrations.embedforge_bridge.list_serial_ports', return_value=[])
@patch('iron.integrations.embedforge_bridge.list_probes', return_value=[])
def test_build_commands_dispatch(
    mock_probes, mock_ports, mock_compile, mock_analyze, mock_run_agent, mock_spinner, mock_ctx
):
    """测试构建命令分发"""
    mock_spinner_instance = MagicMock()
    mock_spinner.return_value = mock_spinner_instance

    # 构建命令应返回 True
    for cmd in COMMAND_GROUPS["build"]:
        assert handle_build_commands(cmd, "", mock_ctx) is True, f"{cmd} 应返回 True"

    # 非构建命令应返回 False
    assert handle_build_commands("/read", "", mock_ctx) is False
    assert handle_build_commands("/model", "", mock_ctx) is False
    assert handle_build_commands("/help", "", mock_ctx) is False


# ── 4. 会话命令分发测试 ───────────────────────────────────────

@patch('iron.cli.commands.session_cmds._show_history')
@patch('iron.cli.commands.session_cmds._resume_session', return_value=None)
@patch('iron.cli.commands.session_cmds.ui.show_status_bar')
def test_session_commands_dispatch(mock_bar, mock_resume, mock_history, mock_ctx):
    """测试会话命令分发"""
    # 会话命令应返回 True
    for cmd in COMMAND_GROUPS["session"]:
        assert handle_session_commands(cmd, "", mock_ctx) is True, f"{cmd} 应返回 True"

    # 非会话命令应返回 False
    assert handle_session_commands("/read", "", mock_ctx) is False
    assert handle_session_commands("/code", "", mock_ctx) is False
    assert handle_session_commands("/model", "", mock_ctx) is False


# ── 5. 系统命令分发测试 ───────────────────────────────────────

@patch('iron.cli.commands.system_cmds._show_rules')
@patch('iron.cli.commands.system_cmds._show_config')
@patch('iron.cli.commands.system_cmds._switch_model', return_value=None)
def test_system_commands_dispatch(mock_switch, mock_config, mock_rules, mock_ctx):
    """测试系统命令分发"""
    # 系统命令应返回 True
    for cmd in COMMAND_GROUPS["system"]:
        assert handle_system_commands(cmd, "", mock_ctx) is True, f"{cmd} 应返回 True"

    # 非系统命令应返回 False
    assert handle_system_commands("/read", "", mock_ctx) is False
    assert handle_system_commands("/code", "", mock_ctx) is False
    assert handle_system_commands("/build", "", mock_ctx) is False


# ── 6. 未知命令测试 ───────────────────────────────────────────

def test_unknown_command(mock_ctx):
    """测试未知命令返回 False"""
    unknown_cmd = "/nonexistent"
    assert handle_file_commands(unknown_cmd, "", mock_ctx) is False
    assert handle_build_commands(unknown_cmd, "", mock_ctx) is False
    assert handle_session_commands(unknown_cmd, "", mock_ctx) is False
    assert handle_system_commands(unknown_cmd, "", mock_ctx) is False


# ── 7. /help 命令测试 ─────────────────────────────────────────

def test_help_command(mock_ctx):
    """测试 /help 命令"""
    with patch('iron.cli.commands.system_cmds.ui.show_help') as mock_show:
        result = handle_system_commands("/help", "", mock_ctx)
        assert result is True
        mock_show.assert_called_once()


# ── 8. /quit 命令测试 ─────────────────────────────────────────

def test_quit_command(mock_ctx):
    """测试 /quit 命令"""
    assert mock_ctx["should_quit"] is False
    result = handle_system_commands("/quit", "", mock_ctx)
    assert result is True
    assert mock_ctx["should_quit"] is True


# ── 9. /clear 命令测试 ────────────────────────────────────────

def test_clear_command(mock_ctx):
    """测试 /clear 命令"""
    with patch('iron.cli.commands.session_cmds.ui.show_status_bar') as mock_bar:
        result = handle_session_commands("/clear", "", mock_ctx)
        assert result is True
        mock_bar.assert_called_once()


# ── 10. 上下文必需字段测试 ────────────────────────────────────

def test_command_ctx_keys(mock_ctx):
    """测试上下文包含必需字段"""
    # 所有 handler 需要的字段并集
    required_keys = {
        "console", "config", "project_root", "llm", "prompt_builder",
        "skills", "last_engine", "session", "loop", "total_rules", "should_quit",
    }
    ctx_keys = set(mock_ctx.keys())
    missing = required_keys - ctx_keys
    assert not missing, f"上下文缺少必需字段: {missing}"
