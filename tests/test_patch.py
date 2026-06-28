"""Patch 工具测试 — P4-2 unified diff 补丁应用

覆盖 PatchTool 的核心逻辑：
- diff 解析（单文件/多文件/hunk 头）
- 补丁验证（成功/文件不存在/行不匹配）
- 补丁应用（新增/删除/修改/多 hunk/dry run/失败）
- 完整 execute 流程

运行方式: pytest tests/test_patch.py -v
"""
import asyncio

import pytest

from iron.tools.patch_tool import PatchTool


# ── diff 解析测试 ──────────────────────────────────────────────

class TestParseDiff:
    """diff 解析测试"""

    def test_parse_simple_diff(self):
        """解析单文件 diff"""
        tool = PatchTool()
        diff = (
            "--- a/src/main.c\n"
            "+++ b/src/main.c\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            " line2\n"
            "+line3\n"
            " line4\n"
        )
        patches = tool._parse_diff(diff)
        assert len(patches) == 1
        assert patches[0]["file"] == "src/main.c"
        assert len(patches[0]["hunks"]) == 1
        hunk = patches[0]["hunks"][0]
        assert hunk["old_start"] == 1
        assert hunk["old_count"] == 3
        assert hunk["new_start"] == 1
        assert hunk["new_count"] == 4
        # 验证行类型
        ops = [op for op, _ in hunk["lines"]]
        assert ops == ["context", "context", "add", "context"]

    def test_parse_multi_file_diff(self):
        """解析多文件 diff"""
        tool = PatchTool()
        diff = (
            "--- a/file1.c\n"
            "+++ b/file1.c\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
            " line2\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        patches = tool._parse_diff(diff)
        assert len(patches) == 2
        assert patches[0]["file"] == "file1.c"
        assert patches[1]["file"] == "file2.py"
        assert len(patches[0]["hunks"]) == 1
        assert len(patches[1]["hunks"]) == 1

    def test_parse_hunk_header(self):
        """解析 hunk 头（含无 count 的简写形式）"""
        tool = PatchTool()
        diff = (
            "--- a/test.txt\n"
            "+++ b/test.txt\n"
            "@@ -1 +1,2 @@\n"
            " line1\n"
            "+line2\n"
            "@@ -5,2 +5,1 @@\n"
            " line5\n"
            "-line6\n"
        )
        patches = tool._parse_diff(diff)
        assert len(patches) == 1
        assert len(patches[0]["hunks"]) == 2
        # 简写形式：-1 等价于 -1,1
        assert patches[0]["hunks"][0]["old_start"] == 1
        assert patches[0]["hunks"][0]["old_count"] == 1
        # 标准形式
        assert patches[0]["hunks"][1]["old_start"] == 5
        assert patches[0]["hunks"][1]["old_count"] == 2


# ── 补丁验证测试 ──────────────────────────────────────────────

class TestVerifyPatch:
    """补丁验证测试"""

    def test_verify_patch_success(self, tmp_path):
        """验证成功"""
        test_file = tmp_path / "test.c"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 2,
                "new_start": 1,
                "new_count": 2,
                "lines": [
                    ("context", "line1"),
                    ("remove", "line2"),
                    ("add", "line2_modified"),
                ],
            }],
        }
        result = tool._verify_patch(patch, str(tmp_path))
        assert result["success"] is True
        assert result["hunks"] == 1

    def test_verify_patch_file_not_exist(self, tmp_path):
        """文件不存在"""
        tool = PatchTool()
        patch = {
            "file": "nonexistent.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 1,
                "new_start": 1,
                "new_count": 1,
                "lines": [("context", "line1")],
            }],
        }
        result = tool._verify_patch(patch, str(tmp_path))
        assert result["success"] is False
        assert "文件不存在" in result["error"]

    def test_verify_patch_line_mismatch(self, tmp_path):
        """行不匹配"""
        test_file = tmp_path / "test.c"
        test_file.write_text("actual_line\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 1,
                "new_start": 1,
                "new_count": 1,
                "lines": [("context", "expected_line")],
            }],
        }
        result = tool._verify_patch(patch, str(tmp_path))
        assert result["success"] is False
        assert "不匹配" in result["error"]


# ── 补丁应用测试 ──────────────────────────────────────────────

class TestApplyPatch:
    """补丁应用测试"""

    def test_apply_patch_add(self, tmp_path):
        """应用新增行"""
        test_file = tmp_path / "test.c"
        test_file.write_text("line1\nline2\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 2,
                "new_start": 1,
                "new_count": 3,
                "lines": [
                    ("context", "line1"),
                    ("add", "inserted"),
                    ("context", "line2"),
                ],
            }],
        }
        result = tool._apply_patch(patch, str(tmp_path))
        assert result["success"] is True
        content = test_file.read_text(encoding="utf-8")
        assert "line1\ninserted\nline2" in content

    def test_apply_patch_remove(self, tmp_path):
        """应用删除行"""
        test_file = tmp_path / "test.c"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 3,
                "new_start": 1,
                "new_count": 2,
                "lines": [
                    ("context", "line1"),
                    ("remove", "line2"),
                    ("context", "line3"),
                ],
            }],
        }
        result = tool._apply_patch(patch, str(tmp_path))
        assert result["success"] is True
        content = test_file.read_text(encoding="utf-8")
        assert "line1\nline3" in content
        assert "line2" not in content

    def test_apply_patch_modify(self, tmp_path):
        """应用修改行"""
        test_file = tmp_path / "test.c"
        test_file.write_text("line1\nold\nline3\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 3,
                "new_start": 1,
                "new_count": 3,
                "lines": [
                    ("context", "line1"),
                    ("remove", "old"),
                    ("add", "new"),
                    ("context", "line3"),
                ],
            }],
        }
        result = tool._apply_patch(patch, str(tmp_path))
        assert result["success"] is True
        content = test_file.read_text(encoding="utf-8")
        assert "line1\nnew\nline3" in content
        assert "old" not in content

    def test_apply_patch_multi_hunk(self, tmp_path):
        """多 hunk 应用"""
        test_file = tmp_path / "test.c"
        test_file.write_text("A\nB\nC\nD\nE\nF\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [
                {
                    "old_start": 2,
                    "old_count": 2,
                    "new_start": 2,
                    "new_count": 2,
                    "lines": [
                        ("remove", "B"),
                        ("add", "B_mod"),
                        ("context", "C"),
                    ],
                },
                {
                    "old_start": 5,
                    "old_count": 2,
                    "new_start": 5,
                    "new_count": 2,
                    "lines": [
                        ("remove", "E"),
                        ("add", "E_mod"),
                        ("context", "F"),
                    ],
                },
            ],
        }
        result = tool._apply_patch(patch, str(tmp_path))
        assert result["success"] is True
        content = test_file.read_text(encoding="utf-8")
        assert "A\nB_mod\nC\nD\nE_mod\nF" in content

    def test_apply_patch_dry_run(self, tmp_path):
        """dry run 不实际修改"""
        test_file = tmp_path / "test.c"
        original = "line1\nline2\n"
        test_file.write_text(original, encoding="utf-8")
        tool = PatchTool()
        diff = (
            "--- a/test.c\n"
            "+++ b/test.c\n"
            "@@ -1,2 +1,2 @@\n"
            " line1\n"
            "-line2\n"
            "+line2_modified\n"
        )
        result = asyncio.run(tool.execute(
            {"diff": diff, "dry_run": True},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is True
        assert result["dry_run"] is True
        # 文件内容未改变
        assert test_file.read_text(encoding="utf-8") == original

    def test_apply_patch_failure(self, tmp_path):
        """应用失败返回错误"""
        test_file = tmp_path / "test.c"
        test_file.write_text("actual\n", encoding="utf-8")
        tool = PatchTool()
        patch = {
            "file": "test.c",
            "hunks": [{
                "old_start": 1,
                "old_count": 1,
                "new_start": 1,
                "new_count": 1,
                "lines": [("context", "expected")],  # 不匹配
            }],
        }
        result = tool._apply_patch(patch, str(tmp_path))
        assert result["success"] is False
        assert "不匹配" in result["error"]


# ── 完整 execute 流程测试 ─────────────────────────────────────

class TestPatchToolExecute:
    """完整 execute 流程测试"""

    def test_patch_tool_execute(self, tmp_path):
        """完整 execute 流程 — 多文件 diff 应用"""
        # 创建两个测试文件
        file1 = tmp_path / "file1.c"
        file1.write_text("old1\nline2\n", encoding="utf-8")
        file2 = tmp_path / "file2.py"
        file2.write_text("old2\nline4\n", encoding="utf-8")

        tool = PatchTool()
        diff = (
            "--- a/file1.c\n"
            "+++ b/file1.c\n"
            "@@ -1,2 +1,2 @@\n"
            "-old1\n"
            "+new1\n"
            " line2\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old2\n"
            "+new2\n"
            " line4\n"
        )
        result = asyncio.run(tool.execute(
            {"diff": diff},
            {"project_dir": str(tmp_path)},
        ))
        assert result["success"] is True
        assert result["applied"] == 2
        assert result["failed"] == 0
        assert file1.read_text(encoding="utf-8") == "new1\nline2\n"
        assert file2.read_text(encoding="utf-8") == "new2\nline4\n"
