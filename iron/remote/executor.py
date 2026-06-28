"""远程执行器 — 本地和 SSH 实现

设计原则：
1. 不引入新依赖（无 paramiko/asyncssh），通过 subprocess 调用 ssh/scp
2. 所有方法都是 async，绝不阻塞事件循环
3. SSHExecutor 通过 stdin 传输文件内容，避免 scp 临时文件
4. 认证完全由 ssh 客户端处理（SSH Agent / 密钥 / 密码提示）

支持的远程格式：
    user@host:/path/to/project
    user@host:22:/path/to/project
    host:/path/to/project（默认当前用户）

实现说明：
    SSHExecutor.read_file: ssh user@host "cat path"
    SSHExecutor.write_file: 通过 stdin 传输：echo content | ssh user@host "cat > path"
    SSHExecutor.run_command: ssh user@host "cd project_path && cmd"
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RemoteSpec:
    """远程连接规格

    由 parse_remote_spec 解析得到。
    """
    user: Optional[str]       # 用户名（None 表示当前用户）
    host: str                 # 主机名或 IP
    port: int                 # 端口（默认 22）
    path: str                 # 项目路径（绝对路径）

    def target(self) -> str:
        """返回 ssh 目标字符串（user@host 或 host）"""
        return f"{self.user}@{self.host}" if self.user else self.host


# 远程规格解析正则：[user@]host[:port]:/path
# 路径必须以 / 开头（绝对路径），避免相对路径歧义
_REMOTE_PATTERN = re.compile(
    r"^(?:(?P<user>[^@:/]+)@)?"        # 可选 user@
    r"(?P<host>[^@:/]+)"               # host（不含 @ : /）
    r"(?::(?P<port>\d+))?"             # 可选 :port
    r":(?P<path>/.*)$"                 # :/path（绝对路径）
)


def parse_remote_spec(spec: str) -> RemoteSpec:
    """解析远程连接字符串

    Args:
        spec: 远程规格字符串，格式 [user@]host[:port]:/path

    Returns:
        RemoteSpec 实例

    Raises:
        ValueError: 格式错误或路径非绝对

    Examples:
        >>> parse_remote_spec("user@host:/path")
        RemoteSpec(user='user', host='host', port=22, path='/path')
        >>> parse_remote_spec("host:2222:/path")
        RemoteSpec(user=None, host='host', port=2222, path='/path')
        >>> parse_remote_spec("host:/path")
        RemoteSpec(user=None, host='host', port=22, path='/path')
    """
    if not spec:
        raise ValueError("远程规格不能为空")

    match = _REMOTE_PATTERN.match(spec)
    if not match:
        raise ValueError(
            f"远程规格格式错误: {spec}\n"
            f"正确格式: [user@]host[:port]:/path（路径必须为绝对路径）"
        )

    user = match.group("user")
    host = match.group("host")
    port_str = match.group("port")
    path = match.group("path")

    try:
        port = int(port_str) if port_str else 22
    except ValueError:
        raise ValueError(f"端口号无效: {port_str}")

    if not (1 <= port <= 65535):
        raise ValueError(f"端口号超出范围（1-65535）: {port}")

    if not path or not path.startswith("/"):
        raise ValueError(f"路径必须为绝对路径: {path}")

    return RemoteSpec(user=user, host=host, port=port, path=path)


# ── 抽象基类 ──────────────────────────────────────────────────────────


class RemoteExecutor(ABC):
    """远程执行器抽象 — 本地和远程实现都遵守此接口

    所有方法都是 async，绝不阻塞事件循环。
    """

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """读取文件内容

        Raises:
            FileNotFoundError: 文件不存在
            RuntimeError: 读取失败
        """
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> bool:
        """写入文件，返回是否成功

        Args:
            path: 文件路径（绝对或相对）
            content: 文件内容

        Returns:
            True 表示写入成功
        """
        ...

    @abstractmethod
    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        """执行命令

        Args:
            cmd: shell 命令字符串
            timeout: 超时秒数

        Returns:
            {"returncode": int, "stdout": str, "stderr": str}
        """
        ...

    @abstractmethod
    async def list_dir(self, path: str) -> list[str]:
        """列出目录内容，返回相对路径列表"""
        ...

    @abstractmethod
    async def file_exists(self, path: str) -> bool:
        """检查文件或目录是否存在"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接（如有）"""
        ...


# ── 本地执行器 ────────────────────────────────────────────────────────


class LocalExecutor(RemoteExecutor):
    """本地执行器 — 直接调用文件系统和 subprocess

    默认执行器，行为与现有代码完全一致。
    """

    def __init__(self, project_root: Optional[str] = None):
        self._project_root = Path(project_root).resolve() if project_root else None

    async def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        if not p.is_file():
            raise RuntimeError(f"不是文件: {path}")
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as e:
            raise RuntimeError(f"读取失败: {e}") from e

    async def write_file(self, path: str, content: str) -> bool:
        p = self._resolve(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return True
        except (OSError, TypeError) as e:
            logger.error("写入文件失败: %s", e)
            return False

    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "命令为空"}

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root) if self._project_root else None,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            return {"returncode": -1, "stdout": "", "stderr": f"启动失败: {e}"}

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            return {
                "returncode": process.returncode if process.returncode is not None else -1,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            }
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            return {"returncode": -1, "stdout": "", "stderr": f"超时（{timeout}s）"}

    async def list_dir(self, path: str) -> list[str]:
        p = self._resolve(path)
        if not p.exists() or not p.is_dir():
            return []
        try:
            return sorted(item.name for item in p.iterdir())
        except OSError:
            return []

    async def file_exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    async def close(self) -> None:
        """本地执行器无需关闭"""
        pass

    def _resolve(self, path: str) -> Path:
        """解析路径为绝对路径

        相对路径基于 project_root 解析（如果有）。
        """
        p = Path(path)
        if not p.is_absolute() and self._project_root:
            p = self._project_root / p
        return p.resolve() if p.is_absolute() or not self._project_root else p.resolve()


# ── SSH 远程执行器 ────────────────────────────────────────────────────


class SSHExecutor(RemoteExecutor):
    """SSH 远程执行器 — 通过 ssh 命令转发

    依赖系统预装的 ssh 客户端（Windows 10+ OpenSSH / Linux / macOS）。
    认证完全由 ssh 处理（SSH Agent / 密钥文件 / 密码提示）。
    不存储任何密码或密钥内容。

    实现：
    - read_file: ssh user@host "cat path"
    - write_file: 通过 stdin 传输，避免 scp 临时文件
    - run_command: ssh user@host "cd project_path && cmd"
    """

    def __init__(self, spec: RemoteSpec, key_file: Optional[str] = None):
        self._spec = spec
        self._key_file = key_file
        self._ssh_available: Optional[bool] = None

    def _build_ssh_cmd(self, remote_cmd: str, *, use_shlex: bool = True) -> list[str]:
        """构建 ssh 命令

        Args:
            remote_cmd: 远程执行的 shell 命令
            use_shlex: True 时用 shlex.quote 包装 remote_cmd

        Returns:
            完整命令列表，如 ["ssh", "-p", "22", "user@host", "cat /path"]
        """
        cmd = ["ssh", "-p", str(self._spec.port)]
        if self._key_file:
            cmd.extend(["-i", self._key_file])
        # 禁用 host key 检查（开发环境友好；生产环境应通过 ~/.ssh/known_hosts）
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.append(self._spec.target())
        # 远程命令用单引号包装，内部用 shlex.quote 防注入
        if use_shlex:
            cmd.append(f"sh -c {shlex.quote(remote_cmd)}")
        else:
            cmd.append(remote_cmd)
        return cmd

    async def _run_ssh(self, remote_cmd: str, input_data: Optional[bytes] = None,
                       timeout: int = 30) -> dict:
        """执行 ssh 命令

        Args:
            remote_cmd: 远程 shell 命令
            input_data: 通过 stdin 传输的数据（None 表示无输入）
            timeout: 超时秒数

        Returns:
            {"returncode": int, "stdout": str, "stderr": str}
        """
        ssh_cmd = self._build_ssh_cmd(remote_cmd)
        try:
            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            return {"returncode": -1, "stdout": "",
                    "stderr": f"ssh 启动失败（请确认 ssh 客户端已安装）: {e}"}

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=input_data),
                timeout=timeout,
            )
            return {
                "returncode": process.returncode if process.returncode is not None else -1,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            }
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            return {"returncode": -1, "stdout": "",
                    "stderr": f"ssh 超时（{timeout}s）"}

    async def check_ssh_available(self) -> bool:
        """检测 ssh 客户端是否可用

        缓存结果，避免重复检测。
        """
        if self._ssh_available is not None:
            return self._ssh_available

        try:
            process = await asyncio.create_subprocess_exec(
                "ssh", "-V",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
            self._ssh_available = process.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            self._ssh_available = False

        return self._ssh_available

    async def read_file(self, path: str) -> str:
        # 用 cat 读取文件，shlex.quote 防注入
        remote_path = shlex.quote(self._resolve_remote(path))
        result = await self._run_ssh(f"cat {remote_path}")

        if result["returncode"] != 0:
            if "No such file" in result["stderr"]:
                raise FileNotFoundError(f"远程文件不存在: {path}")
            raise RuntimeError(f"读取远程文件失败: {result['stderr']}")

        return result["stdout"]

    async def write_file(self, path: str, content: str) -> bool:
        # 通过 stdin 传输内容，避免命令行长度限制
        remote_path = shlex.quote(self._resolve_remote(path))
        # 先创建父目录，再用 cat 写入
        remote_cmd = f"mkdir -p $(dirname {remote_path}) && cat > {remote_path}"
        input_data = content.encode("utf-8")
        result = await self._run_ssh(remote_cmd, input_data=input_data)

        if result["returncode"] != 0:
            logger.error("写入远程文件失败: %s", result["stderr"])
            return False
        return True

    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "命令为空"}

        # 在 project_path 下执行命令
        project_path = shlex.quote(self._spec.path)
        full_cmd = f"cd {project_path} && {cmd}"
        return await self._run_ssh(full_cmd, timeout=timeout)

    async def list_dir(self, path: str) -> list[str]:
        remote_path = shlex.quote(self._resolve_remote(path))
        # 用 ls -1A 列出（每行一个，含隐藏文件，不含 . 和 ..）
        result = await self._run_ssh(f"ls -1A {remote_path}")

        if result["returncode"] != 0:
            return []

        return [line.strip() for line in result["stdout"].splitlines() if line.strip()]

    async def file_exists(self, path: str) -> bool:
        remote_path = shlex.quote(self._resolve_remote(path))
        # test -e 检查存在性（文件或目录均可）
        result = await self._run_ssh(f"test -e {remote_path}")
        return result["returncode"] == 0

    async def close(self) -> None:
        """SSH 通过子进程调用，无需显式关闭连接"""
        pass

    def _resolve_remote(self, path: str) -> str:
        """解析远程路径为绝对路径

        相对路径基于 spec.path 解析。
        """
        if not path:
            return self._spec.path
        if path.startswith("/"):
            return path
        # 拼接 spec.path 和相对路径
        base = self._spec.path.rstrip("/")
        return f"{base}/{path}"


# ── 工厂函数 ──────────────────────────────────────────────────────────


def create_executor(spec: Optional[str] = None,
                    project_root: Optional[str] = None,
                    key_file: Optional[str] = None) -> RemoteExecutor:
    """根据规格创建执行器

    Args:
        spec: 远程规格字符串（None 或空字符串表示本地）
        project_root: 本地项目根目录（仅 LocalExecutor 使用）
        key_file: SSH 私钥文件路径（仅 SSHExecutor 使用）

    Returns:
        - spec 为空: LocalExecutor
        - spec 非空: SSHExecutor

    Raises:
        ValueError: spec 格式错误
    """
    if not spec:
        return LocalExecutor(project_root)

    remote_spec = parse_remote_spec(spec)
    return SSHExecutor(remote_spec, key_file=key_file)
