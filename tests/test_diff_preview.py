"""Diff 预览测试（v4.0 Track 6）

覆盖：
- _render_diff 函数（unified_diff + 颜色 + 截断）
- edit_file 工具集成 diff 预览（context 注入 console 时触发）
- 特性门控注册
- engine _get_console 懒加载
"""
import asyncio
from io import StringIO

import pytest
from rich.console import Console

from iron.cli.ui import _render_diff


def _run_async(coro):
    return asyncio.run(coro)


# ── _render_diff 函数测试 ──────────────────────────────────────────

class TestRenderDiff:
    def test_no_changes(self):
        """无变更时输出 '无变更'"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\nb\n", "a\nb\n", "test.txt")
        output = buf.getvalue()
        assert "无变更" in output

    def test_simple_diff(self):
        """简单 diff 应包含 +/- 行和文件名"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\nb\n", "a\nc\n", "test.txt")
        output = buf.getvalue()
        assert "Diff" in output or "test.txt" in output
        # unified_diff 输出 -b 和 +c
        assert "-b" in output
        assert "+c" in output

    def test_long_diff_truncated(self):
        """长 diff 应截断并显示 '省略' 提示"""
        old = "\n".join([f"line{i}" for i in range(100)])
        new = "\n".join([f"line{i}_modified" for i in range(100)])
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, old, new, "big.txt")
        output = buf.getvalue()
        assert "省略" in output

    def test_no_file_path(self):
        """未传 file_path 时不崩溃"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\n", "b\n", "")
        # 应有输出（即使没文件名）
        assert buf.getvalue()

    def test_diff_with_addition_only(self):
        """纯新增行的 diff"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\n", "a\nb\n", "add.txt")
        output = buf.getvalue()
        assert "+b" in output

    def test_diff_with_deletion_only(self):
        """纯删除行的 diff"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\nb\n", "a\n", "del.txt")
        output = buf.getvalue()
        assert "-b" in output

    def test_diff_includes_unified_markers(self):
        """unified_diff 头部应包含 +++/--- 标记"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\n", "b\n", "marker.txt")
        output = buf.getvalue()
        assert "+++" in output
        assert "---" in output

    def test_diff_below_threshold_not_truncated(self):
        """小于 50 行 diff 不截断"""
        # 20 行变更 → 约 43 行 diff（2 头部 + 1 hunk + 40 +/-），不触发截断
        old = "\n".join([f"line{i}" for i in range(20)])
        new = "\n".join([f"line{i}_x" for i in range(20)])
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, old, new, "boundary.txt")
        output = buf.getvalue()
        # 不应触发截断
        assert "省略" not in output

    def test_diff_over_50_lines_truncated(self):
        """51 行 diff 触发截断"""
        old = "\n".join([f"line{i}" for i in range(48)])
        new = "\n".join([f"line{i}_x" for i in range(48)])
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, old, new, "over.txt")
        output = buf.getvalue()
        assert "省略" in output


# ── edit_file 集成测试 ────────────────────────────────────────────

class TestEditFileDiffPreview:
    def test_diff_preview_triggered_with_console(self, tmp_path):
        """传入 console 时应触发 diff 预览（输出含 Diff 字样）"""
        from iron.tools.edit_file import EditFileTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        buf = StringIO()
        console = Console(file=buf, width=80)
        tool = EditFileTool()
        result = _run_async(tool.execute(
            {"path": str(test_file), "old_string": "hello",
             "new_string": "hi"},
            {"project_dir": str(tmp_path), "console": console},
        ))
        assert result["success"] is True
        output = buf.getvalue()
        # 应触发 diff 预览（features.diff_preview 默认 True）
        assert "Diff" in output or "diff" in output.lower() or "+++" in output

    def test_diff_preview_not_triggered_without_console(self, tmp_path):
        """未传 console 时不崩溃，编辑正常完成"""
        from iron.tools.edit_file import EditFileTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        tool = EditFileTool()
        # 不传 console
        result = _run_async(tool.execute(
            {"path": str(test_file), "old_string": "hello",
             "new_string": "hi"},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is True

    def test_edit_file_still_writes_correctly(self, tmp_path):
        """diff 预览不影响文件写入"""
        from iron.tools.edit_file import EditFileTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        buf = StringIO()
        console = Console(file=buf, width=80)
        tool = EditFileTool()
        _run_async(tool.execute(
            {"path": str(test_file), "old_string": "hello",
             "new_string": "hi"},
            {"project_dir": str(tmp_path), "console": console},
        ))
        # 文件应被正确写入
        assert test_file.read_text(encoding="utf-8") == "hi world"

    def test_edit_file_diff_preview_failure_not_blocking(self, tmp_path, monkeypatch):
        """diff 预览失败不应阻塞编辑"""
        from iron.tools.edit_file import EditFileTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        # 用一个会抛异常的 console
        class _BrokenConsole:
            def print(self, *a, **kw):
                raise RuntimeError("broken console")

        tool = EditFileTool()
        # _render_diff 内部 console.print 抛异常会被 edit_file 的 try/except 兜住
        # 但 _BrokenConsole.print 直接抛 RuntimeError，被 except (ImportError, RuntimeError, ValueError) 兜住
        result = _run_async(tool.execute(
            {"path": str(test_file), "old_string": "hello",
             "new_string": "hi"},
            {"project_dir": str(tmp_path), "console": _BrokenConsole()},
        ))
        assert result["success"] is True
        # 文件仍应被写入
        assert test_file.read_text(encoding="utf-8") == "hi world"


# ── 特性门控测试 ──────────────────────────────────────────────────

class TestFeatureGate:
    def test_diff_preview_feature_registered(self):
        from iron.config.features import DEFAULT_FEATURES
        assert "diff_preview" in DEFAULT_FEATURES
        assert DEFAULT_FEATURES["diff_preview"] is True

    def test_diff_preview_enabled_by_default(self):
        from iron.config.features import is_feature_enabled
        assert is_feature_enabled("diff_preview") is True

    def test_diff_preview_can_be_disabled(self):
        """特性可被关闭"""
        from iron.config.features import FeatureFlags
        flags = FeatureFlags()
        assert flags.disable("diff_preview") is True
        assert flags.is_enabled("diff_preview") is False
        # 恢复
        flags.enable("diff_preview")


# ── engine _get_console 懒加载测试 ───────────────────────────────

class TestEngineGetConsole:
    def test_get_console_returns_instance(self):
        from iron.agent.engine import CoderAgentEngine
        from types import SimpleNamespace
        # 用最小 mock config 构造 engine（不调用真实 LLM）
        # 直接测试 _get_console 方法
        engine = CoderAgentEngine.__new__(CoderAgentEngine)
        engine._console = None
        console = engine._get_console()
        # 应返回 Console 实例（rich 可用时）
        if console is not None:
            from rich.console import Console as _C
            assert isinstance(console, _C)

    def test_get_console_caches_instance(self):
        """多次调用返回同一实例"""
        from iron.agent.engine import CoderAgentEngine
        engine = CoderAgentEngine.__new__(CoderAgentEngine)
        engine._console = None
        c1 = engine._get_console()
        c2 = engine._get_console()
        if c1 is not None:
            assert c1 is c2

    def test_get_console_uses_injected_console(self):
        """测试可直接注入 console"""
        from iron.agent.engine import CoderAgentEngine
        from rich.console import Console as _C
        engine = CoderAgentEngine.__new__(CoderAgentEngine)
        injected = _C()
        engine._console = injected
        assert engine._get_console() is injected
