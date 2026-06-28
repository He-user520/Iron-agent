"""Git 工具集测试（v4.0 Track 5）

覆盖：
- _run_git / _is_git_repo 辅助函数
- 5 个工具类的 execute（async）
- register_git_tools 批量注册
- /git 命令分发
- 特性门控注册
"""
import asyncio
import subprocess
from io import StringIO

import pytest
from rich.console import Console

from iron.tools.git_tools import (
    GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool,
    register_git_tools, _run_git, _is_git_repo,
)


# ── fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """创建临时 git 仓库（无提交）"""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(tmp_path), check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def git_repo_with_commit(git_repo):
    """创建带一个提交的 git 仓库"""
    (git_repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(git_repo),
                   check=True, capture_output=True)
    return git_repo


def _run_async(coro):
    """同步运行 async 协程（测试辅助）"""
    return asyncio.run(coro)


# ── 辅助函数测试 ──────────────────────────────────────────────────

class TestRunGit:
    def test_run_git_success(self, git_repo):
        result = _run_git(str(git_repo), ["status", "--short"])
        assert result["success"] is True
        assert result["returncode"] == 0

    def test_run_git_not_a_repo(self, tmp_path):
        result = _run_git(str(tmp_path), ["status"])
        assert result["success"] is False
        assert result["returncode"] != 0

    def test_run_git_not_installed(self, monkeypatch):
        """模拟 git 未安装"""
        import iron.tools.git_tools as gt
        # monkeypatch subprocess.run 抛 FileNotFoundError
        def _fake_run(*a, **kw):
            raise FileNotFoundError("git")
        monkeypatch.setattr(gt.subprocess, "run", _fake_run)
        result = _run_git(".", ["status"])
        assert result["success"] is False
        assert "未安装" in result["stderr"]

    def test_is_git_repo_true(self, git_repo):
        assert _is_git_repo(str(git_repo)) is True

    def test_is_git_repo_false(self, tmp_path):
        assert _is_git_repo(str(tmp_path)) is False


# ── GitStatusTool ────────────────────────────────────────────────

class TestGitStatusTool:
    def test_clean_repo(self, git_repo_with_commit):
        tool = GitStatusTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "干净" in result["output"]

    def test_dirty_repo(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new", encoding="utf-8")
        tool = GitStatusTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "new.txt" in result["output"]

    def test_not_a_repo(self, tmp_path):
        tool = GitStatusTool()
        result = _run_async(tool.execute({}, {"project_dir": str(tmp_path)}))
        assert result["success"] is False
        assert "git 仓库" in result["error"]

    def test_schema_format(self):
        """schema 符合 OpenAI function calling 格式"""
        tool = GitStatusTool()
        s = tool.schema
        assert s["type"] == "function"
        assert s["function"]["name"] == "git_status"
        assert "parameters" in s["function"]


# ── GitDiffTool ──────────────────────────────────────────────────

class TestGitDiffTool:
    def test_no_diff(self, git_repo_with_commit):
        tool = GitDiffTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "无 diff" in result["output"]

    def test_unstaged_diff(self, git_repo_with_commit):
        (git_repo_with_commit / "README.md").write_text("# Modified\n", encoding="utf-8")
        tool = GitDiffTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        # diff 输出应包含 +Modified 或 README
        assert "Modified" in result["output"] or "README" in result["output"]

    def test_staged_diff(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new", encoding="utf-8")
        subprocess.run(["git", "add", "new.txt"], cwd=str(git_repo_with_commit),
                       check=True, capture_output=True)
        tool = GitDiffTool()
        result = _run_async(tool.execute({"staged": True},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "new.txt" in result["output"]

    def test_diff_specific_path(self, git_repo_with_commit):
        (git_repo_with_commit / "a.txt").write_text("a", encoding="utf-8")
        (git_repo_with_commit / "b.txt").write_text("b", encoding="utf-8")
        tool = GitDiffTool()
        result = _run_async(tool.execute({"path": "a.txt"},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True

    def test_not_a_repo(self, tmp_path):
        tool = GitDiffTool()
        result = _run_async(tool.execute({}, {"project_dir": str(tmp_path)}))
        assert result["success"] is False


# ── GitLogTool ───────────────────────────────────────────────────

class TestGitLogTool:
    def test_log_with_commit(self, git_repo_with_commit):
        tool = GitLogTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "init" in result["output"]

    def test_log_limit(self, git_repo_with_commit):
        # 添加几个提交
        for i in range(3):
            (git_repo_with_commit / f"f{i}.txt").write_text(str(i), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(git_repo_with_commit),
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"commit {i}"],
                           cwd=str(git_repo_with_commit), check=True, capture_output=True)
        tool = GitLogTool()
        result = _run_async(tool.execute({"limit": 2},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        # 应只返回 2 条
        lines = [l for l in result["output"].splitlines() if l.strip()]
        assert len(lines) <= 2

    def test_log_empty_repo(self, git_repo):
        """空仓库：git log 失败，应返回友好错误"""
        tool = GitLogTool()
        result = _run_async(tool.execute({}, {"project_dir": str(git_repo)}))
        assert result["success"] is False
        # stderr 含 "does not have any commits" 或自定义 "无提交历史"
        assert result["error"]


# ── GitAddTool ───────────────────────────────────────────────────

class TestGitAddTool:
    def test_add_file(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new", encoding="utf-8")
        tool = GitAddTool()
        result = _run_async(tool.execute({"paths": ["new.txt"]},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "1" in result["output"]

    def test_add_multiple_files(self, git_repo_with_commit):
        (git_repo_with_commit / "a.txt").write_text("a", encoding="utf-8")
        (git_repo_with_commit / "b.txt").write_text("b", encoding="utf-8")
        tool = GitAddTool()
        result = _run_async(tool.execute({"paths": ["a.txt", "b.txt"]},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True
        assert "2" in result["output"]

    def test_add_empty_paths(self, git_repo_with_commit):
        tool = GitAddTool()
        result = _run_async(tool.execute({"paths": []},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is False
        assert "paths" in result["error"]


# ── GitCommitTool ────────────────────────────────────────────────

class TestGitCommitTool:
    def test_commit_requires_permission(self):
        tool = GitCommitTool()
        assert tool.requires_permission is True

    def test_commit_success(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new", encoding="utf-8")
        # 先 add
        add_tool = GitAddTool()
        _run_async(add_tool.execute({"paths": ["new.txt"]},
                                     {"project_dir": str(git_repo_with_commit)}))
        tool = GitCommitTool()
        result = _run_async(tool.execute({"message": "add new file"},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is True

    def test_commit_empty_message(self, git_repo_with_commit):
        tool = GitCommitTool()
        result = _run_async(tool.execute({"message": ""},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is False
        assert "空" in result["error"]

    def test_commit_whitespace_message(self, git_repo_with_commit):
        tool = GitCommitTool()
        result = _run_async(tool.execute({"message": "   "},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is False

    def test_commit_nothing_staged(self, git_repo_with_commit):
        """无暂存变更时 git commit 会失败"""
        tool = GitCommitTool()
        result = _run_async(tool.execute({"message": "empty commit"},
                                         {"project_dir": str(git_repo_with_commit)}))
        assert result["success"] is False


# ── register_git_tools ───────────────────────────────────────────

class TestRegisterGitTools:
    def test_register_all(self):
        from iron.tools.registry import ToolRegistry
        reg = ToolRegistry()
        register_git_tools(reg)
        names = reg.tool_names()
        assert "git_status" in names
        assert "git_diff" in names
        assert "git_log" in names
        assert "git_add" in names
        assert "git_commit" in names

    def test_register_count(self):
        from iron.tools.registry import ToolRegistry
        reg = ToolRegistry()
        register_git_tools(reg)
        # 5 个 Git 工具
        git_names = [n for n in reg.tool_names() if n.startswith("git_")]
        assert len(git_names) == 5


# ── /git 命令分发 ─────────────────────────────────────────────────

class TestGitCommand:
    def test_handle_git_unknown_subcommand(self):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": "."}
        # 未知子命令
        result = handle_git_commands("/git", "unknown_subcmd", ctx)
        assert result is True
        output = buf.getvalue()
        assert "未知子命令" in output

    def test_handle_git_wrong_command(self):
        from iron.cli.commands.git_cmds import handle_git_commands
        # 传入非 /git 命令，应返回 False
        result = handle_git_commands("/help", "", {"console": Console()})
        assert result is False

    def test_handle_git_status(self, git_repo_with_commit):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": str(git_repo_with_commit)}
        result = handle_git_commands("/git", "status", ctx)
        assert result is True
        # 干净仓库应输出 ✓
        assert "✓" in buf.getvalue() or "干净" in buf.getvalue()

    def test_handle_git_no_args_defaults_status(self, git_repo_with_commit):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": str(git_repo_with_commit)}
        # 无子命令 → 默认 status
        result = handle_git_commands("/git", "", ctx)
        assert result is True

    def test_handle_git_log_with_limit(self, git_repo_with_commit):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": str(git_repo_with_commit)}
        result = handle_git_commands("/git", "log 5", ctx)
        assert result is True
        assert "init" in buf.getvalue()

    def test_handle_git_add_no_paths(self, git_repo_with_commit):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": str(git_repo_with_commit)}
        result = handle_git_commands("/git", "add", ctx)
        assert result is True
        assert "用法" in buf.getvalue()

    def test_handle_git_commit_no_message(self, git_repo_with_commit):
        from iron.cli.commands.git_cmds import handle_git_commands
        buf = StringIO()
        console = Console(file=buf, width=80)
        ctx = {"console": console, "project_root": str(git_repo_with_commit)}
        result = handle_git_commands("/git", "commit", ctx)
        assert result is True
        assert "用法" in buf.getvalue()


# ── 特性门控注册 ──────────────────────────────────────────────────

class TestFeatureGate:
    def test_git_tools_feature_registered(self):
        from iron.config.features import DEFAULT_FEATURES
        assert "git_tools" in DEFAULT_FEATURES
        assert DEFAULT_FEATURES["git_tools"] is True

    def test_diff_preview_feature_registered(self):
        from iron.config.features import DEFAULT_FEATURES
        assert "diff_preview" in DEFAULT_FEATURES
        assert DEFAULT_FEATURES["diff_preview"] is True

    def test_git_tools_enabled_by_default(self):
        from iron.config.features import is_feature_enabled
        assert is_feature_enabled("git_tools") is True
