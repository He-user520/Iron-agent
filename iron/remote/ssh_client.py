"""SSH 客户端封装 — 提供连接测试和高级操作

这是 executor.py 的补充，提供：
- SSH 连接测试（ping）
- 批量文件同步（rsync 风格）
- 远程进程检查

不引入新依赖，全部通过 subprocess 调用 ssh。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from iron.remote.executor import RemoteSpec, SSHExecutor, parse_remote_spec

logger = logging.getLogger(__name__)


class SSHClient:
    """SSH 客户端 — 封装 SSHExecutor 提供高级功能

    用法：
        client = SSHClient("user@host:/path")
        if await client.ping():
            files = await client.list_dir(".")
    """

    def __init__(self, spec: str, key_file: Optional[str] = None):
        self._spec: RemoteSpec = parse_remote_spec(spec)
        self._executor = SSHExecutor(self._spec, key_file=key_file)

    @property
    def spec(self) -> RemoteSpec:
        return self._spec

    @property
    def executor(self) -> SSHExecutor:
        return self._executor

    async def ping(self, timeout: int = 10) -> bool:
        """测试 SSH 连接是否可用

        执行简单的 echo 命令，检查返回码。
        """
        result = await self._executor.run_command("echo __IRON_PING_OK__", timeout=timeout)
        if result["returncode"] != 0:
            return False
        return "__IRON_PING_OK__" in result["stdout"]

    async def check_ssh_available(self) -> bool:
        """检测 ssh 客户端是否安装"""
        return await self._executor.check_ssh_available()

    async def read_file(self, path: str) -> str:
        return await self._executor.read_file(path)

    async def write_file(self, path: str, content: str) -> bool:
        return await self._executor.write_file(path, content)

    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        return await self._executor.run_command(cmd, timeout=timeout)

    async def list_dir(self, path: str) -> list[str]:
        return await self._executor.list_dir(path)

    async def file_exists(self, path: str) -> bool:
        return await self._executor.file_exists(path)

    async def close(self) -> None:
        await self._executor.close()


async def test_ssh_connection(spec: str, timeout: int = 10) -> bool:
    """便捷函数：测试 SSH 连接

    Args:
        spec: 远程规格字符串（user@host:/path）
        timeout: 超时秒数

    Returns:
        True 表示连接成功
    """
    try:
        client = SSHClient(spec)
        return await client.ping(timeout=timeout)
    except (ValueError, RuntimeError, OSError):
        return False
