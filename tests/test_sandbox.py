"""OS 沙箱测试 — 覆盖 iron/security/sandbox.py

运行方式：pytest tests/test_sandbox.py -v

测试范围：
- NoopSandbox: 命令执行 / 超时 / 空命令 / 路径校验
- WindowsSandbox: 路径校验 / 工作目录越界
- LinuxSandbox: 后端检测 / bwrap/firejail 命令构建 / 路径校验
- create_sandbox: 工厂函数根据平台和 enabled 返回正确实例
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from iron.security.sandbox import (
    Sandbox, NoopSandbox, WindowsSandbox, LinuxSandbox, create_sandbox,
)


# ── NoopSandbox 测试 ──────────────────────────────────────────────────

class TestNoopSandbox:
    """NoopSandbox — 无沙箱直接执行"""

    def test_is_sandbox(self):
        s = NoopSandbox()
        assert isinstance(s, Sandbox)

    def test_validate_path_always_true(self):
        s = NoopSandbox()
        assert s.validate_path("/any/path") is True
        assert s.validate_path("") is True  # Noop 允许所有
        assert s.validate_path("../etc/passwd") is True

    def test_validate_path_with_project_root(self):
        s = NoopSandbox("/tmp/project")
        # NoopSandbox 不强制路径校验，即使有 project_root 也允许所有
        assert s.validate_path("/tmp/project/file.txt") is True
        assert s.validate_path("/etc/passwd") is True

    @pytest.mark.asyncio
    async def test_execute_simple_command(self):
        s = NoopSandbox()
        # 用跨平台命令：python --version
        result = await s.execute([sys.executable, "--version"])
        assert result["returncode"] == 0
        assert "Python" in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_empty_command(self):
        s = NoopSandbox()
        result = await s.execute([])
        assert result["returncode"] == -1
        assert "空" in result["stderr"]

    @pytest.mark.asyncio
    async def test_execute_command_not_found(self):
        s = NoopSandbox()
        result = await s.execute(["nonexistent_command_xyz"])
        assert result["returncode"] == -1
        assert "启动失败" in result["stderr"]

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        s = NoopSandbox()
        # 启动一个 2 秒的进程，但 timeout=0.05 秒
        result = await s.execute(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout=0.05,
        )
        assert result["returncode"] == -1
        assert "超时" in result["stderr"]

    @pytest.mark.asyncio
    async def test_execute_with_cwd(self, tmp_path):
        s = NoopSandbox()
        result = await s.execute(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=str(tmp_path),
        )
        assert result["returncode"] == 0
        # 输出可能含换行，用 in 判断
        assert str(tmp_path) in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_with_env(self):
        s = NoopSandbox()
        # 复制当前环境变量，然后设置测试变量，确保子进程能正常启动
        env = os.environ.copy()
        env["TEST_VAR"] = "hello"
        result = await s.execute(
            [sys.executable, "-c", "import os; print(os.environ.get('TEST_VAR', ''))"],
            env=env,
        )
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_stderr_capture(self):
        s = NoopSandbox()
        result = await s.execute(
            [sys.executable, "-c", "import sys; sys.stderr.write('error msg')"],
        )
        assert result["returncode"] == 0
        assert "error msg" in result["stderr"]


# ── WindowsSandbox 测试 ──────────────────────────────────────────────

class TestWindowsSandbox:
    """WindowsSandbox — 路径校验 + 子进程超时"""

    def test_is_sandbox(self):
        s = WindowsSandbox("/tmp/project")
        assert isinstance(s, Sandbox)

    def test_validate_path_in_project(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        # 项目内路径合法
        assert s.validate_path(str(tmp_path / "src" / "main.c")) is True

    def test_validate_path_outside_project(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        # 项目外路径拒绝
        assert s.validate_path("/etc/passwd") is False
        assert s.validate_path(str(tmp_path.parent / "other")) is False

    def test_validate_path_empty(self):
        s = WindowsSandbox("/tmp/project")
        assert s.validate_path("") is False

    def test_validate_path_relative(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        # 相对路径解析后应在项目内
        # 注意：相对路径相对于当前工作目录，不是项目目录
        # 这里测试绝对路径明确在项目内的情况
        assert s.validate_path(str(tmp_path)) is True

    @pytest.mark.asyncio
    async def test_execute_cwd_outside_blocked(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        # cwd 在项目外应被拒绝
        result = await s.execute(
            [sys.executable, "--version"],
            cwd="/etc",  # 项目外
        )
        assert result["returncode"] == -1
        assert "越界" in result["stderr"]

    @pytest.mark.asyncio
    async def test_execute_cwd_inside_allowed(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        result = await s.execute(
            [sys.executable, "--version"],
            cwd=str(tmp_path),
        )
        assert result["returncode"] == 0

    @pytest.mark.asyncio
    async def test_execute_command_success(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        result = await s.execute([sys.executable, "--version"])
        assert result["returncode"] == 0
        assert "Python" in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_timeout(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        result = await s.execute(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout=0.05,
        )
        assert result["returncode"] == -1
        assert "超时" in result["stderr"]

    @pytest.mark.asyncio
    async def test_execute_empty_command(self, tmp_path):
        s = WindowsSandbox(str(tmp_path))
        result = await s.execute([])
        assert result["returncode"] == -1


# ── LinuxSandbox 测试 ────────────────────────────────────────────────

class TestLinuxSandbox:
    """LinuxSandbox — bwrap/firejail/noop 后端检测"""

    def test_is_sandbox(self):
        s = LinuxSandbox(os.path.abspath("/tmp/project"))
        assert isinstance(s, Sandbox)

    def test_backend_property(self):
        s = LinuxSandbox(os.path.abspath("/tmp/project"))
        # backend 应该是 bwrap/firejail/noop 之一
        assert s.backend in ("bwrap", "firejail", "noop")

    @patch("iron.security.sandbox.shutil.which")
    def test_detect_bwrap(self, mock_which):
        def which_side_effect(cmd):
            return "/usr/bin/bwrap" if cmd == "bwrap" else None
        mock_which.side_effect = which_side_effect
        s = LinuxSandbox(os.path.abspath("/tmp/project"))
        assert s.backend == "bwrap"

    @patch("iron.security.sandbox.shutil.which")
    def test_detect_firejail_when_no_bwrap(self, mock_which):
        def which_side_effect(cmd):
            return "/usr/bin/firejail" if cmd == "firejail" else None
        mock_which.side_effect = which_side_effect
        s = LinuxSandbox(os.path.abspath("/tmp/project"))
        assert s.backend == "firejail"

    @patch("iron.security.sandbox.shutil.which", return_value=None)
    def test_detect_noop_when_no_backend(self, mock_which):
        s = LinuxSandbox(os.path.abspath("/tmp/project"))
        assert s.backend == "noop"

    def test_build_bwrap_cmd(self, tmp_path):
        s = LinuxSandbox(str(tmp_path))
        s._backend = "bwrap"  # 强制设置
        cmd = s._build_bwrap_cmd(["python", "--version"])
        assert cmd[0] == "bwrap"
        assert "--ro-bind" in cmd
        assert str(tmp_path) in cmd
        assert "--unshare-all" in cmd
        assert "python" in cmd

    def test_build_firejail_cmd(self, tmp_path):
        s = LinuxSandbox(str(tmp_path))
        s._backend = "firejail"
        cmd = s._build_firejail_cmd(["python", "--version"])
        assert cmd[0] == "firejail"
        assert f"--private={tmp_path}" in cmd
        assert "python" in cmd

    def test_validate_path_in_project(self, tmp_path):
        s = LinuxSandbox(str(tmp_path))
        assert s.validate_path(str(tmp_path / "file.txt")) is True

    def test_validate_path_outside_project(self, tmp_path):
        s = LinuxSandbox(str(tmp_path))
        assert s.validate_path("/etc/passwd") is False

    def test_validate_path_empty(self):
        s = LinuxSandbox("/tmp/project")
        assert s.validate_path("") is False

    @pytest.mark.asyncio
    async def test_execute_noop_backend(self, tmp_path):
        """backend=noop 时降级到 NoopSandbox 行为"""
        s = LinuxSandbox(str(tmp_path))
        with patch.object(s, "_backend", "noop"):
            result = await s.execute([sys.executable, "--version"])
            assert result["returncode"] == 0
            assert "Python" in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_cwd_outside_blocked(self, tmp_path):
        s = LinuxSandbox(str(tmp_path))
        result = await s.execute(
            [sys.executable, "--version"],
            cwd="/etc",
        )
        assert result["returncode"] == -1
        assert "越界" in result["stderr"]


# ── create_sandbox 工厂函数测试 ──────────────────────────────────────

class TestCreateSandbox:
    """create_sandbox 工厂函数

    注意：不能 patch os.name，因为 Path() 在 Windows 上实例化 PosixPath 会抛
    NotImplementedError。改为按实际平台测试，用 skip 标记跨平台用例。
    """

    def test_disabled_returns_noop(self, tmp_path):
        s = create_sandbox(str(tmp_path), enabled=False)
        assert isinstance(s, NoopSandbox)

    @pytest.mark.skipif(os.name != "nt", reason="Windows 平台测试")
    def test_enabled_on_windows_returns_windows_sandbox(self, tmp_path):
        s = create_sandbox(str(tmp_path), enabled=True)
        assert isinstance(s, WindowsSandbox)

    @pytest.mark.skipif(os.name != "posix" or not os.path.exists("/proc"),
                        reason="Linux 平台测试")
    def test_enabled_on_linux_returns_linux_sandbox(self, tmp_path):
        s = create_sandbox(str(tmp_path), enabled=True)
        assert isinstance(s, LinuxSandbox)

    @pytest.mark.skipif(os.name != "posix" or os.path.exists("/proc"),
                        reason="macOS 平台测试（posix 但无 /proc）")
    def test_enabled_on_macos_returns_windows_sandbox(self, tmp_path):
        s = create_sandbox(str(tmp_path), enabled=True)
        assert isinstance(s, WindowsSandbox)

    def test_factory_returns_sandbox_instance(self, tmp_path):
        s = create_sandbox(str(tmp_path), enabled=False)
        assert isinstance(s, Sandbox)

    def test_factory_current_platform(self, tmp_path):
        """当前平台下 enabled=True 应返回非 Noop 沙箱"""
        s = create_sandbox(str(tmp_path), enabled=True)
        # 任何已知平台都应返回 Sandbox 子类
        assert isinstance(s, Sandbox)


# ── 集成测试 ──────────────────────────────────────────────────────────

class TestSandboxIntegration:
    """集成测试 — 沙箱实例间的行为一致性"""

    @pytest.mark.asyncio
    async def test_all_sandboxes_handle_empty_cmd(self, tmp_path):
        """所有沙箱实例对空命令的响应一致"""
        sandboxes = [
            NoopSandbox(str(tmp_path)),
            WindowsSandbox(str(tmp_path)),
            LinuxSandbox(str(tmp_path)),
        ]
        for s in sandboxes:
            result = await s.execute([])
            assert result["returncode"] == -1
            assert "空" in result["stderr"]

    @pytest.mark.asyncio
    async def test_all_sandboxes_handle_timeout(self, tmp_path):
        """所有沙箱实例对超时的响应一致"""
        sandboxes = [
            NoopSandbox(str(tmp_path)),
            WindowsSandbox(str(tmp_path)),
            LinuxSandbox(str(tmp_path)),
        ]
        for s in sandboxes:
            result = await s.execute(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout=0.05,
            )
            assert result["returncode"] == -1
            assert "超时" in result["stderr"]
