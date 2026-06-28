"""Track 10: tree-sitter 安装引导测试

测试用例：
- test_doctor_shows_tree_sitter_section: doctor 显示 tree-sitter 详细信息
- test_doctor_shows_install_hint_when_missing: 未安装时显示安装命令
- test_code_indexer_init_already_installed: init 时已安装跳过 subprocess
- test_code_indexer_init_runs_pip_when_missing: init 时未安装触发 pip
- test_code_indexer_init_pip_failure_friendly_error: pip 失败时友好提示
- test_code_indexer_init_enables_feature: init 后特性 code_indexer=True
- test_code_indexer_init_feature_save_failure: 特性保存失败时友好提示
- test_code_indexer_status_shows_installed: status 已安装状态
- test_code_indexer_status_shows_missing: status 未安装状态
- test_code_indexer_degradation_hint: CodeIndexer 降级时提示安装命令

运行方式: pytest tests/test_code_indexer_bootstrap.py -v
"""
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from iron.config.features import (
    FeatureFlags,
    get_feature_flags,
    reset_global_flags,
    is_feature_enabled,
)


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_global_singleton():
    """每个测试前后重置全局单例，避免污染"""
    reset_global_flags()
    yield
    reset_global_flags()


@pytest.fixture
def tmp_features_file(tmp_path, monkeypatch):
    """让全局单例使用临时配置文件，避免污染 ~/.iron/features.yml"""
    cfg_path = tmp_path / "features.yml"
    # 修改 DEFAULT_FEATURES_FILE 让 get_feature_flags() 首次加载指向临时文件
    import iron.config.features as feat_mod
    monkeypatch.setattr(feat_mod, "DEFAULT_FEATURES_FILE", cfg_path)
    # 预创建一个默认实例
    reset_global_flags()
    flags = get_feature_flags()
    # 把实例的 config_path 也指向临时文件
    flags.config_path = cfg_path
    return cfg_path


@pytest.fixture
def runner():
    """Click CliRunner"""
    return CliRunner()


# ── 1. doctor 命令的 tree-sitter 检测 ────────────────────────────

class TestDoctorTreeSitterDetection:
    """Step 1: iron doctor 显示 tree-sitter 详细信息 + 安装命令"""

    def test_doctor_shows_tree_sitter_section(self, runner, tmp_features_file):
        """doctor 输出包含 tree-sitter 详细检测段"""
        from iron.cli.main import cli
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        # 必须含 tree-sitter 关键字（无论装没装都会输出）
        assert "tree-sitter" in result.output.lower() or "tree_sitter" in result.output.lower()

    def test_doctor_shows_install_hint_when_missing(self, runner, tmp_features_file):
        """未安装时 doctor 输出安装命令 + iron code-indexer init 提示"""
        from iron.cli.main import cli
        # mock tree_sitter 未安装
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return ""
            # 其他模块用原逻辑
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import):
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "tree-sitter" in result.output.lower()
        # 必须出现安装命令
        assert "pip install" in result.output.lower()
        assert "iron code-indexer init" in result.output

    def test_doctor_shows_feature_status(self, runner, tmp_features_file):
        """doctor 输出包含 code_indexer 特性状态行"""
        from iron.cli.main import cli
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "code_indexer" in result.output


# ── 2. code-indexer init 命令 ────────────────────────────────────

class TestCodeIndexerInit:
    """Step 2: iron code-indexer init 一键安装 + 启用"""

    def test_init_already_installed(self, runner, tmp_features_file):
        """tree-sitter 已安装时跳过 subprocess，直接启用特性"""
        from iron.cli.main import cli
        # mock tree_sitter 已安装
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return "0.22.0"
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import), \
             patch("subprocess.run") as mock_run:
            result = runner.invoke(cli, ["code-indexer", "init"])
        assert result.exit_code == 0
        # 未调用 subprocess（已安装就跳过）
        mock_run.assert_not_called()
        # 特性已启用
        assert is_feature_enabled("code_indexer") is True
        assert "已安装" in result.output or "已就绪" in result.output

    def test_init_runs_pip_when_missing(self, runner, tmp_features_file):
        """tree-sitter 未安装时触发 pip install"""
        from iron.cli.main import cli
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return ""
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import), \
             patch("subprocess.run") as mock_run:
            result = runner.invoke(cli, ["code-indexer", "init"])
        assert result.exit_code == 0
        # subprocess 被调用一次（pip install）
        assert mock_run.call_count == 1
        args, kwargs = mock_run.call_args
        cmd = args[0] if args else kwargs.get("args")
        assert "pip" in " ".join(cmd).lower() or "install" in " ".join(cmd)
        # 特性已启用
        assert is_feature_enabled("code_indexer") is True

    def test_init_pip_failure_friendly_error(self, runner, tmp_features_file):
        """pip 安装失败时给出友好提示并退出码 1"""
        from iron.cli.main import cli
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return ""
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "pip")):
            result = runner.invoke(cli, ["code-indexer", "init"])
        # 非零退出码（sys.exit(1)）
        assert result.exit_code != 0
        # 输出包含友好提示
        assert "安装失败" in result.output or "手动安装" in result.output


# ── 3. code-indexer status 命令 ──────────────────────────────────

class TestCodeIndexerStatus:
    """Step 2: iron code-indexer status 查看状态"""

    def test_status_shows_installed(self, runner, tmp_features_file):
        """已安装时 status 显示 ✓"""
        from iron.cli.main import cli
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return "0.22.0"
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import):
            result = runner.invoke(cli, ["code-indexer", "status"])
        assert result.exit_code == 0
        assert "已安装" in result.output

    def test_status_shows_missing(self, runner, tmp_features_file):
        """未安装时 status 显示 ✗ + 安装提示"""
        from iron.cli.main import cli
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return ""
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import):
            result = runner.invoke(cli, ["code-indexer", "status"])
        assert result.exit_code == 0
        assert "未安装" in result.output
        assert "iron code-indexer init" in result.output

    def test_status_shows_feature_state(self, runner, tmp_features_file):
        """status 显示 code_indexer 特性状态"""
        from iron.cli.main import cli
        result = runner.invoke(cli, ["code-indexer", "status"])
        assert result.exit_code == 0
        assert "code_indexer" in result.output


# ── 4. 特性启用失败友好提示 ───────────────────────────────────────

class TestFeatureEnableFailure:
    """Step 2: 特性 save() 失败时的降级路径"""

    def test_init_feature_save_failure_friendly_hint(self, runner, tmp_features_file):
        """features.save() 抛 OSError 时给出手动编辑提示"""
        from iron.cli.main import cli
        def _fake_check_import(name):
            if name in ("tree_sitter", "tree_sitter_c"):
                return "0.22.0"
            try:
                mod = __import__(name)
                return getattr(mod, "__version__", "已安装")
            except ImportError:
                return ""
        # mock FeatureFlags.save 抛 OSError
        with patch("iron.cli.main._check_import", side_effect=_fake_check_import), \
             patch("iron.config.features.FeatureFlags.save", side_effect=OSError("disk full")):
            result = runner.invoke(cli, ["code-indexer", "init"])
        # init 不应崩溃，退出码 0（save 失败不阻断）
        assert result.exit_code == 0
        assert "启用特性失败" in result.output or "手动编辑" in result.output


# ── 5. CodeIndexer 降级提示 ───────────────────────────────────────

class TestCodeIndexerDegradation:
    """Step 3: CodeIndexer 降级时记录安装/启用命令到日志"""

    def test_degradation_logs_install_hint(self, tmp_path, caplog):
        """tree-sitter 未安装时 _check_tree_sitter 返回 False 并记录安装命令"""
        import logging
        from iron.core.db import Database
        from iron.integrations.code_indexer import CodeIndexer

        # mock ImportError 触发降级路径
        import builtins
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name in ("tree_sitter", "tree_sitter_c"):
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            db_path = tmp_path / "test.db"
            db = Database(db_path=db_path)
            db.connect()
            try:
                with caplog.at_level(logging.INFO, logger="iron.integrations.code_indexer"):
                    indexer = CodeIndexer(db, str(tmp_path))
                # 降级标志
                assert indexer.available is False
                # 日志含安装/启用命令
                messages = " ".join(r.message for r in caplog.records)
                assert "tree-sitter" in messages.lower() or "tree_sitter" in messages
                assert "pip install" in messages
                assert "code-indexer init" in messages
            finally:
                db.close()
