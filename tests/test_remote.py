"""远程执行器测试 — 覆盖 iron/remote/executor.py + ssh_client.py

运行方式：pytest tests/test_remote.py -v

测试策略：
- parse_remote_spec: 纯字符串解析，无 IO
- LocalExecutor: 实际文件系统操作（用 tmp_path）
- SSHExecutor: mock _run_ssh 避免真实 SSH 连接
- SSHClient: mock ping 避免真实连接
- create_executor: 工厂函数
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from iron.remote.executor import (
    RemoteExecutor, LocalExecutor, SSHExecutor,
    RemoteSpec, parse_remote_spec, create_executor,
)
from iron.remote.ssh_client import SSHClient
from iron.remote.ssh_client import test_ssh_connection as _check_ssh


# ── parse_remote_spec 测试 ────────────────────────────────────────────

class TestParseRemoteSpec:
    """远程规格解析"""

    def test_user_host_path(self):
        spec = parse_remote_spec("user@host:/path/to/project")
        assert spec.user == "user"
        assert spec.host == "host"
        assert spec.port == 22
        assert spec.path == "/path/to/project"

    def test_host_only(self):
        spec = parse_remote_spec("host:/path")
        assert spec.user is None
        assert spec.host == "host"
        assert spec.port == 22
        assert spec.path == "/path"

    def test_with_port(self):
        spec = parse_remote_spec("user@host:2222:/path")
        assert spec.user == "user"
        assert spec.host == "host"
        assert spec.port == 2222
        assert spec.path == "/path"

    def test_host_with_port_no_user(self):
        spec = parse_remote_spec("host:2222:/path")
        assert spec.user is None
        assert spec.port == 2222

    def test_ip_address(self):
        spec = parse_remote_spec("user@192.168.1.1:/path")
        assert spec.host == "192.168.1.1"
        assert spec.user == "user"

    def test_ip_with_port(self):
        spec = parse_remote_spec("10.0.0.1:2222:/path")
        assert spec.host == "10.0.0.1"
        assert spec.port == 2222
        assert spec.user is None

    def test_target_with_user(self):
        spec = parse_remote_spec("user@host:/path")
        assert spec.target() == "user@host"

    def test_target_without_user(self):
        spec = parse_remote_spec("host:/path")
        assert spec.target() == "host"

    def test_empty_spec_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            parse_remote_spec("")

    def test_relative_path_raises(self):
        with pytest.raises(ValueError, match="格式错误|绝对路径"):
            parse_remote_spec("user@host:relative/path")

    def test_missing_path_raises(self):
        with pytest.raises(ValueError):
            parse_remote_spec("user@host")

    def test_missing_host_raises(self):
        with pytest.raises(ValueError):
            parse_remote_spec(":/path")

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError):
            parse_remote_spec("user@host:abc:/path")

    def test_port_out_of_range_raises(self):
        with pytest.raises(ValueError, match="范围"):
            parse_remote_spec("user@host:99999:/path")

    def test_port_zero_raises(self):
        with pytest.raises(ValueError, match="范围"):
            parse_remote_spec("user@host:0:/path")

    def test_negative_port_raises(self):
        # -1 的负号不被 \d+ 匹配，会先触发格式错误
        with pytest.raises(ValueError):
            parse_remote_spec("user@host:-1:/path")


# ── LocalExecutor 测试 ──────────────────────────────────────────────

class TestLocalExecutor:
    """本地执行器"""

    def test_is_remote_executor(self):
        ex = LocalExecutor()
        assert isinstance(ex, RemoteExecutor)

    def test_init_with_project_root(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        assert ex._project_root == tmp_path.resolve()

    def test_init_without_project_root(self):
        ex = LocalExecutor()
        assert ex._project_root is None

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        # 创建测试文件
        (tmp_path / "test.txt").write_text("hello world", encoding="utf-8")
        ex = LocalExecutor(str(tmp_path))
        content = await ex.read_file("test.txt")
        assert content == "hello world"

    @pytest.mark.asyncio
    async def test_read_file_absolute_path(self, tmp_path):
        f = tmp_path / "abs.txt"
        f.write_text("absolute", encoding="utf-8")
        ex = LocalExecutor()
        content = await ex.read_file(str(f))
        assert content == "absolute"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            await ex.read_file("nonexistent.txt")

    @pytest.mark.asyncio
    async def test_read_file_is_directory(self, tmp_path):
        (tmp_path / "dir").mkdir()
        ex = LocalExecutor(str(tmp_path))
        with pytest.raises(RuntimeError, match="不是文件"):
            await ex.read_file("dir")

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.write_file("new.txt", "content")
        assert result is True
        assert (tmp_path / "new.txt").read_text() == "content"

    @pytest.mark.asyncio
    async def test_write_file_creates_parent_dirs(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.write_file("sub/dir/file.txt", "nested")
        assert result is True
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    @pytest.mark.asyncio
    async def test_write_file_overwrite(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("old", encoding="utf-8")
        ex = LocalExecutor(str(tmp_path))
        await ex.write_file("existing.txt", "new")
        assert f.read_text() == "new"

    @pytest.mark.asyncio
    async def test_run_command_success(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.run_command("echo hello")
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_run_command_failure(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.run_command("exit 1")
        assert result["returncode"] == 1

    @pytest.mark.asyncio
    async def test_run_command_empty(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.run_command("")
        assert result["returncode"] == -1
        assert "空" in result["stderr"]

    @pytest.mark.asyncio
    async def test_run_command_timeout(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        result = await ex.run_command("sleep 2", timeout=0.05)
        assert result["returncode"] == -1
        assert "超时" in result["stderr"]

    @pytest.mark.asyncio
    async def test_list_dir(self, tmp_path):
        (tmp_path / "file1.txt").write_text("1")
        (tmp_path / "file2.txt").write_text("2")
        (tmp_path / "subdir").mkdir()
        ex = LocalExecutor(str(tmp_path))
        items = await ex.list_dir(".")
        assert "file1.txt" in items
        assert "file2.txt" in items
        assert "subdir" in items

    @pytest.mark.asyncio
    async def test_list_dir_empty(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        items = await ex.list_dir(".")
        assert items == []

    @pytest.mark.asyncio
    async def test_list_dir_nonexistent(self, tmp_path):
        ex = LocalExecutor(str(tmp_path))
        items = await ex.list_dir("nonexistent")
        assert items == []

    @pytest.mark.asyncio
    async def test_file_exists(self, tmp_path):
        (tmp_path / "exists.txt").write_text("yes")
        ex = LocalExecutor(str(tmp_path))
        assert await ex.file_exists("exists.txt") is True
        assert await ex.file_exists("nonexistent.txt") is False

    @pytest.mark.asyncio
    async def test_close_no_error(self):
        ex = LocalExecutor()
        await ex.close()  # 不应抛异常


# ── SSHExecutor 测试（mock _run_ssh）─────────────────────────────────

class TestSSHExecutor:
    """SSH 执行器（不实际连接）"""

    def test_is_remote_executor(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        assert isinstance(ex, RemoteExecutor)

    def test_init_with_spec(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        assert ex._spec is spec

    def test_init_with_key_file(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec, key_file="/path/to/key")
        assert ex._key_file == "/path/to/key"

    def test_build_ssh_cmd_basic(self):
        spec = RemoteSpec(user="user", host="host", port=22, path="/p")
        ex = SSHExecutor(spec)
        cmd = ex._build_ssh_cmd("ls")
        assert cmd[0] == "ssh"
        assert "-p" in cmd
        assert "22" in cmd
        assert "user@host" in cmd

    def test_build_ssh_cmd_with_port(self):
        spec = RemoteSpec(user="u", host="h", port=2222, path="/p")
        ex = SSHExecutor(spec)
        cmd = ex._build_ssh_cmd("ls")
        assert "2222" in cmd

    def test_build_ssh_cmd_with_key_file(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec, key_file="/key")
        cmd = ex._build_ssh_cmd("ls")
        assert "-i" in cmd
        assert "/key" in cmd

    def test_resolve_remote_absolute(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/project")
        ex = SSHExecutor(spec)
        assert ex._resolve_remote("/etc/passwd") == "/etc/passwd"

    def test_resolve_remote_relative(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/project")
        ex = SSHExecutor(spec)
        assert ex._resolve_remote("src/main.c") == "/project/src/main.c"

    def test_resolve_remote_empty(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/project")
        ex = SSHExecutor(spec)
        assert ex._resolve_remote("") == "/project"

    def test_resolve_remote_trailing_slash(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/project/")
        ex = SSHExecutor(spec)
        assert ex._resolve_remote("file.txt") == "/project/file.txt"

    @pytest.mark.asyncio
    async def test_read_file_success(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 0, "stdout": "file content", "stderr": ""
            }
            content = await ex.read_file("/path/file.txt")
            assert content == "file content"
            mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_read_file_not_found(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 1, "stdout": "",
                "stderr": "No such file or directory"
            }
            with pytest.raises(FileNotFoundError):
                await ex.read_file("/nonexistent")

    @pytest.mark.asyncio
    async def test_read_file_other_error(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 1, "stdout": "", "stderr": "Permission denied"
            }
            with pytest.raises(RuntimeError, match="读取远程文件失败"):
                await ex.read_file("/file")

    @pytest.mark.asyncio
    async def test_write_file_success(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"returncode": 0, "stdout": "", "stderr": ""}
            result = await ex.write_file("/path/file.txt", "content")
            assert result is True

    @pytest.mark.asyncio
    async def test_write_file_failure(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 1, "stdout": "", "stderr": "Permission denied"
            }
            result = await ex.write_file("/file", "content")
            assert result is False

    @pytest.mark.asyncio
    async def test_run_command_success(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/project")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 0, "stdout": "output", "stderr": ""
            }
            result = await ex.run_command("ls -la")
            assert result["returncode"] == 0
            # 命令应在 project_path 下执行
            called_cmd = mock_run.call_args[0][0]
            assert "/project" in called_cmd
            assert "ls -la" in called_cmd

    @pytest.mark.asyncio
    async def test_run_command_empty(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        result = await ex.run_command("")
        assert result["returncode"] == -1
        assert "空" in result["stderr"]

    @pytest.mark.asyncio
    async def test_list_dir_success(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 0, "stdout": "file1.txt\nfile2.txt\ndir1\n", "stderr": ""
            }
            items = await ex.list_dir("/path")
            assert items == ["file1.txt", "file2.txt", "dir1"]

    @pytest.mark.asyncio
    async def test_list_dir_failure(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 1, "stdout": "", "stderr": "No such directory"
            }
            items = await ex.list_dir("/nonexistent")
            assert items == []

    @pytest.mark.asyncio
    async def test_file_exists_true(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"returncode": 0, "stdout": "", "stderr": ""}
            assert await ex.file_exists("/file") is True

    @pytest.mark.asyncio
    async def test_file_exists_false(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        with patch.object(ex, "_run_ssh", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"returncode": 1, "stdout": "", "stderr": ""}
            assert await ex.file_exists("/file") is False

    @pytest.mark.asyncio
    async def test_close_no_error(self):
        spec = RemoteSpec(user="u", host="h", port=22, path="/p")
        ex = SSHExecutor(spec)
        await ex.close()  # 不应抛异常


# ── create_executor 工厂函数测试 ─────────────────────────────────────

class TestCreateExecutor:
    """create_executor 工厂"""

    def test_no_spec_returns_local(self):
        ex = create_executor()
        assert isinstance(ex, LocalExecutor)

    def test_empty_spec_returns_local(self):
        ex = create_executor("")
        assert isinstance(ex, LocalExecutor)

    def test_none_spec_returns_local(self):
        ex = create_executor(None)
        assert isinstance(ex, LocalExecutor)

    def test_valid_spec_returns_ssh(self):
        ex = create_executor("user@host:/path")
        assert isinstance(ex, SSHExecutor)

    def test_local_with_project_root(self, tmp_path):
        ex = create_executor(project_root=str(tmp_path))
        assert isinstance(ex, LocalExecutor)
        assert ex._project_root == tmp_path.resolve()

    def test_ssh_with_key_file(self):
        ex = create_executor("user@host:/path", key_file="/key")
        assert isinstance(ex, SSHExecutor)
        assert ex._key_file == "/key"

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError):
            create_executor("invalid-spec")


# ── SSHClient 测试 ───────────────────────────────────────────────────

class TestSSHClient:
    """SSH 客户端封装"""

    def test_init_with_valid_spec(self):
        client = SSHClient("user@host:/path")
        assert client.spec.user == "user"
        assert client.spec.host == "host"

    def test_init_with_invalid_spec_raises(self):
        with pytest.raises(ValueError):
            SSHClient("invalid")

    @pytest.mark.asyncio
    async def test_ping_success(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 0,
                "stdout": "__IRON_PING_OK__",
                "stderr": "",
            }
            assert await client.ping() is True

    @pytest.mark.asyncio
    async def test_ping_failure_returncode(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 1, "stdout": "", "stderr": "connection refused"
            }
            assert await client.ping() is False

    @pytest.mark.asyncio
    async def test_ping_failure_no_marker(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "returncode": 0, "stdout": "other output", "stderr": ""
            }
            assert await client.ping() is False

    @pytest.mark.asyncio
    async def test_read_file_delegates(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "read_file", new_callable=AsyncMock) as mock_read:
            mock_read.return_value = "content"
            content = await client.read_file("/file")
            assert content == "content"

    @pytest.mark.asyncio
    async def test_write_file_delegates(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "write_file", new_callable=AsyncMock) as mock_write:
            mock_write.return_value = True
            assert await client.write_file("/file", "content") is True

    @pytest.mark.asyncio
    async def test_run_command_delegates(self):
        client = SSHClient("user@host:/path")
        with patch.object(client._executor, "run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"returncode": 0, "stdout": "ok", "stderr": ""}
            result = await client.run_command("ls")
            assert result["stdout"] == "ok"


# ── test_ssh_connection 便捷函数测试 ──────────────────────────────────

class TestTestSSHConnection:
    """test_ssh_connection 便捷函数"""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        with patch("iron.remote.ssh_client.SSHClient") as MockClient:
            instance = MockClient.return_value
            instance.ping = AsyncMock(return_value=True)
            result = await _check_ssh("user@host:/path")
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_value_error(self):
        with patch("iron.remote.ssh_client.SSHClient", side_effect=ValueError):
            result = await _check_ssh("invalid")
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_runtime_error(self):
        with patch("iron.remote.ssh_client.SSHClient", side_effect=RuntimeError):
            result = await _check_ssh("user@host:/path")
            assert result is False
