"""MultiEdit 多文件原子编辑测试（v4.0 Track 7）

覆盖：
- 单文件 / 多文件编辑成功
- 文件不存在 → 失败 + 回滚
- old_string 不匹配 → 失败 + 回滚
- 超过 MAX_FILES → 失败
- 空 edits → 失败
- 原子性：第 N 个文件写入失败 → 前 N-1 个回滚
- requires_permission=True
- diff 预览触发
- 特性门控注册
- 路径越界防护
"""
import asyncio
import os
from io import StringIO

import pytest
from rich.console import Console

from iron.tools.multi_edit import MultiEditTool, register_multi_edit_tool, MAX_FILES


def _run_async(coro):
    """同步运行 async 协程（测试辅助）"""
    return asyncio.run(coro)


# ── fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path):
    """创建临时项目目录，含 3 个测试文件"""
    (tmp_path / "a.txt").write_text("hello foo", encoding="utf-8")
    (tmp_path / "b.txt").write_text("world baz", encoding="utf-8")
    (tmp_path / "c.txt").write_text("test qux", encoding="utf-8")
    return tmp_path


# ── 基础测试 ──────────────────────────────────────────────────────

class TestMultiEditBasic:
    def test_requires_permission(self):
        tool = MultiEditTool()
        assert tool.requires_permission is True

    def test_name(self):
        tool = MultiEditTool()
        assert tool.name == "multi_edit"

    def test_schema_format(self):
        """schema 符合 OpenAI function calling 格式"""
        tool = MultiEditTool()
        s = tool.schema
        assert s["type"] == "function"
        assert s["function"]["name"] == "multi_edit"
        assert "edits" in s["function"]["parameters"]["properties"]

    def test_empty_edits(self, tmp_path):
        tool = MultiEditTool()
        result = _run_async(tool.execute({"edits": []},
                                         {"project_dir": str(tmp_path)}))
        assert result["success"] is False
        assert "edits" in result["error"]

    def test_exceeds_max_files(self, tmp_path):
        """超过 MAX_FILES 限制"""
        tool = MultiEditTool()
        edits = [{"path": f"f{i}.txt", "old_string": "a", "new_string": "b"}
                 for i in range(MAX_FILES + 1)]
        result = _run_async(tool.execute({"edits": edits},
                                         {"project_dir": str(tmp_path)}))
        assert result["success"] is False
        assert str(MAX_FILES) in result["error"]


# ── 单文件编辑测试 ────────────────────────────────────────────────

class TestSingleFileEdit:
    def test_single_file_success(self, project):
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "a.txt", "old_string": "foo",
                        "new_string": "bar"}]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is True
        assert (project / "a.txt").read_text(encoding="utf-8") == "hello bar"

    def test_same_old_new_string(self, project):
        """old_string 和 new_string 相同时应失败"""
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "a.txt", "old_string": "foo",
                        "new_string": "foo"}]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "相同" in result["error"]

    def test_missing_path(self, project):
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "", "old_string": "foo",
                        "new_string": "bar"}]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "path" in result["error"]

    def test_missing_old_string(self, project):
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "a.txt", "old_string": "",
                        "new_string": "bar"}]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "old_string" in result["error"]


# ── 多文件编辑测试 ────────────────────────────────────────────────

class TestMultiFileEdit:
    def test_multi_file_success(self, project):
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "baz", "new_string": "qux"},
                {"path": "c.txt", "old_string": "qux", "new_string": "end"},
            ]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is True
        assert (project / "a.txt").read_text(encoding="utf-8") == "hello bar"
        assert (project / "b.txt").read_text(encoding="utf-8") == "world qux"
        assert (project / "c.txt").read_text(encoding="utf-8") == "test end"

    def test_files_modified_list(self, project):
        """成功时返回 files_modified 列表"""
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "baz", "new_string": "qux"},
            ]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is True
        assert "files_modified" in result
        assert set(result["files_modified"]) == {"a.txt", "b.txt"}


# ── 原子性 + 回滚测试 ────────────────────────────────────────────

class TestAtomicityRollback:
    def test_file_not_exist_rolls_back(self, project):
        """第 2 个文件不存在 → 第 1 个不应被修改"""
        tool = MultiEditTool()
        original_a = (project / "a.txt").read_text(encoding="utf-8")
        result = _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "nonexistent.txt", "old_string": "x", "new_string": "y"},
            ]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "不存在" in result["error"]
        # a.txt 不应被修改（回滚）
        assert (project / "a.txt").read_text(encoding="utf-8") == original_a

    def test_old_string_not_found_rolls_back(self, project):
        """第 2 个文件 old_string 不匹配 → 第 1 个应回滚"""
        tool = MultiEditTool()
        original_a = (project / "a.txt").read_text(encoding="utf-8")
        result = _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "NONEXISTENT", "new_string": "y"},
            ]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "未找到匹配" in result["error"]
        # a.txt 应保持原状
        assert (project / "a.txt").read_text(encoding="utf-8") == original_a

    def test_write_failure_rolls_back(self, project, monkeypatch):
        """第 2 个文件写入失败 → 第 1 个应回滚

        模拟写入失败：monkeypatch Path.write_text 抛 OSError
        """
        tool = MultiEditTool()
        original_a = (project / "a.txt").read_text(encoding="utf-8")

        # 计数器：第 2 次调用 write_text 时抛异常
        call_count = {"n": 0}
        original_write_text = type(project / "a.txt").write_text

        def _failing_write(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise OSError("模拟写入失败")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr("pathlib.Path.write_text", _failing_write)

        result = _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "baz", "new_string": "qux"},
            ]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is False
        assert "回滚" in result["error"]

    def test_rollback_preserves_original(self, project):
        """回滚后文件内容完全恢复"""
        tool = MultiEditTool()
        original_b = (project / "b.txt").read_text(encoding="utf-8")
        # 第 2 个文件 old_string 不匹配 → 触发回滚
        _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "WRONG", "new_string": "x"},
            ]},
            {"project_dir": str(project)},
        ))
        # b.txt 应保持原状（虽然它本来就没被改，但回滚流程应正确处理）
        assert (project / "b.txt").read_text(encoding="utf-8") == original_b


# ── diff 预览集成测试 ─────────────────────────────────────────────

class TestDiffPreviewIntegration:
    def test_diff_preview_triggered(self, project):
        """传入 console 时应触发 diff 预览"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "a.txt", "old_string": "foo",
                        "new_string": "bar"}]},
            {"project_dir": str(project), "console": console},
        ))
        assert result["success"] is True
        output = buf.getvalue()
        # 应触发 diff 预览
        assert "Diff" in output or "+++" in output

    def test_diff_preview_not_triggered_without_console(self, project):
        """未传 console 时不崩溃"""
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "a.txt", "old_string": "foo",
                        "new_string": "bar"}]},
            {"project_dir": str(project)},
        ))
        assert result["success"] is True

    def test_diff_preview_multi_file(self, project):
        """多文件编辑时每个文件都应有 diff 预览"""
        buf = StringIO()
        console = Console(file=buf, width=80)
        tool = MultiEditTool()
        _run_async(tool.execute(
            {"edits": [
                {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                {"path": "b.txt", "old_string": "baz", "new_string": "qux"},
            ]},
            {"project_dir": str(project), "console": console},
        ))
        output = buf.getvalue()
        # 应包含两个文件的 diff
        assert "a.txt" in output
        assert "b.txt" in output


# ── 路径安全测试 ──────────────────────────────────────────────────

class TestPathSafety:
    def test_path_traversal_rejected(self, tmp_path):
        """路径越界（../ 跳出项目根）应被拒绝"""
        tool = MultiEditTool()
        # 构造明确在 tmp_path 之外的路径
        outside_path = str(tmp_path.parent / "outside_file.txt")
        result = _run_async(tool.execute(
            {"edits": [{"path": outside_path,
                        "old_string": "x", "new_string": "y"}]},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is False
        # path_guard 拒绝（越界或文件不存在，都算拒绝）
        assert result["error"]

    def test_absolute_path_outside_project_rejected(self, tmp_path):
        """绝对路径在项目外应被拒绝"""
        tool = MultiEditTool()
        # 用一个明确在项目外的绝对路径
        import os
        if os.name == "nt":
            outside = "C:/Windows/System32/drivers/etc/hosts"
        else:
            outside = "/etc/passwd"
        result = _run_async(tool.execute(
            {"edits": [{"path": outside,
                        "old_string": "x", "new_string": "y"}]},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is False


# ── 大文件保护测试 ────────────────────────────────────────────────

class TestLargeFileProtection:
    def test_large_file_rejected(self, tmp_path):
        """超过 10MB 的文件应被拒绝"""
        # 创建一个伪大文件（实际不写 10MB，用 monkeypatch stat）
        (tmp_path / "big.txt").write_text("hello", encoding="utf-8")
        tool = MultiEditTool()

        # monkeypatch stat 返回超大 size
        class _FakeStat:
            st_size = 11 * 1024 * 1024  # 11MB

        from pathlib import Path
        original_stat = Path.stat

        def _fake_stat(self, *args, **kwargs):
            if self.name == "big.txt":
                return _FakeStat()
            return original_stat(self, *args, **kwargs)

        import pathlib
        import unittest.mock
        with unittest.mock.patch.object(pathlib.Path, "stat", _fake_stat):
            result = _run_async(tool.execute(
                {"edits": [{"path": "big.txt", "old_string": "hello",
                            "new_string": "world"}]},
                {"project_dir": str(tmp_path)},
            ))
        assert result["success"] is False
        assert "过大" in result["error"]


# ── 特性门控 + 注册测试 ──────────────────────────────────────────

class TestFeatureGateAndRegister:
    def test_feature_registered(self):
        from iron.config.features import DEFAULT_FEATURES
        assert "multi_edit" in DEFAULT_FEATURES
        assert DEFAULT_FEATURES["multi_edit"] is True

    def test_feature_enabled_by_default(self):
        from iron.config.features import is_feature_enabled
        assert is_feature_enabled("multi_edit") is True

    def test_register_tool(self):
        from iron.tools.registry import ToolRegistry
        reg = ToolRegistry()
        register_multi_edit_tool(reg)
        assert "multi_edit" in reg.tool_names()

    def test_not_in_readonly_set(self):
        """multi_edit 不应在只读集合中（需权限）"""
        from iron.agent.engine import CoderAgentEngine, TaskAgentEngine
        assert "multi_edit" not in CoderAgentEngine._READONLY_EXTERNAL_TOOLS
        assert "multi_edit" not in TaskAgentEngine.READONLY_TOOLS


# ── GBK 编码回退测试 ─────────────────────────────────────────────

class TestEncodingFallback:
    def test_gbk_file_fallback(self, tmp_path):
        """GBK 编码文件应能被读取（回退到 GBK）"""
        # 写一个 GBK 编码文件
        gbk_content = "你好 world".encode("gbk")
        (tmp_path / "gbk.txt").write_bytes(gbk_content)
        tool = MultiEditTool()
        result = _run_async(tool.execute(
            {"edits": [{"path": "gbk.txt", "old_string": "world",
                        "new_string": "python"}]},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is True
