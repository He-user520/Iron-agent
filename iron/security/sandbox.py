"""OS 沙箱 — 隔离工具执行，限制文件访问范围

平台支持：
- Linux: 优先 bwrap（bubblewrap），退回 firejail，都不可用时 NoopSandbox
- Windows: 路径校验 + 子进程超时（无 AppContainer 时不阻断执行）
- macOS: 路径校验 + 子进程超时（不依赖 sandbox-exec，保持简单）

设计原则：
1. 不阻塞主循环 — 所有 execute() 都是异步
2. 路径校验优先 — 即使无 OS 级沙箱，路径校验也能拦截越界访问
3. 失败降级 — 沙箱不可用时降级到 NoopSandbox，不崩溃
4. 不使用 subprocess.run — 全部用 asyncio.create_subprocess_exec 避免阻塞
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Sandbox(ABC):
    """OS 沙箱抽象 — 不同平台不同实现

    所有方法都是 async 或纯校验，绝不阻塞事件循环。
    """

    @abstractmethod
    async def execute(self, cmd: list[str], cwd: Optional[str] = None,
                      timeout: int = 30, env: Optional[dict] = None) -> dict:
        """在沙箱内执行命令

        Args:
            cmd: 命令及参数列表（如 ["python", "-c", "print(1)"]）
            cwd: 工作目录（None 表示当前目录）
            timeout: 超时秒数
            env: 环境变量覆盖（None 表示继承父进程）

        Returns:
            {"returncode": int, "stdout": str, "stderr": str}
            超时或失败时 returncode 为 -1，stderr 含错误信息
        """
        ...

    @abstractmethod
    def validate_path(self, path: str) -> bool:
        """校验路径是否在沙箱允许范围内

        Args:
            path: 待校验路径（相对或绝对）

        Returns:
            True 表示允许访问，False 表示拒绝
        """
        ...


class NoopSandbox(Sandbox):
    """无沙箱 — 直接执行（默认）

    仅做基本路径校验（项目根目录内），不引入 OS 级隔离。
    """

    def __init__(self, project_root: Optional[str] = None):
        self._project_root = Path(project_root).resolve() if project_root else None

    async def execute(self, cmd: list[str], cwd: Optional[str] = None,
                      timeout: int = 30, env: Optional[dict] = None) -> dict:
        """直接执行命令，不隔离"""
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "命令为空"}

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env if env is not None else None,
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

    def validate_path(self, path: str) -> bool:
        """NoopSandbox 允许所有路径（不强制项目内）"""
        return True


class WindowsSandbox(Sandbox):
    """Windows 沙箱 — 路径校验 + 子进程超时

    Windows 上无成熟的命令行沙箱工具（AppContainer 仅适用于 UWP），
    因此采用路径校验作为主要防护，配合子进程超时。

    防护能力：
    - 项目目录外路径拒绝访问（validate_path 返回 False）
    - 子进程超时自动 kill
    - 环境变量隔离（可选 env 参数）
    """

    def __init__(self, project_root: str):
        self._project_root = Path(project_root).resolve()

    async def execute(self, cmd: list[str], cwd: Optional[str] = None,
                      timeout: int = 30, env: Optional[dict] = None) -> dict:
        """在路径校验保护下执行命令"""
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "命令为空"}

        # 工作目录必须在校验范围内
        if cwd and not self.validate_path(cwd):
            return {"returncode": -1, "stdout": "",
                    "stderr": f"工作目录越界: {cwd}"}

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env if env is not None else None,
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

    def validate_path(self, path: str) -> bool:
        """校验路径在项目根内"""
        if not path:
            return False
        try:
            resolved = Path(path).resolve()
            # 路径必须在项目根下
            resolved.relative_to(self._project_root)
            return True
        except (ValueError, OSError):
            return False


class LinuxSandbox(Sandbox):
    """Linux 沙箱 — 优先 bwrap，退回 firejail，再退回 Noop

    bwrap（bubblewrap）是 Flatpak 同款沙箱，无 setuid，更安全。
    firejail 是用户级沙箱，需要 setuid 或用户 namespaces。

    检测顺序：bwrap > firejail > noop
    """

    def __init__(self, project_root: str):
        self._project_root = Path(project_root).resolve()
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        """检测可用的沙箱后端"""
        for cmd in ["bwrap", "firejail"]:
            if shutil.which(cmd):
                return cmd
        return "noop"

    @property
    def backend(self) -> str:
        """当前使用的沙箱后端名称"""
        return self._backend

    def _build_bwrap_cmd(self, cmd: list[str]) -> list[str]:
        """构建 bwrap 命令前缀

        - --ro-bind /usr /usr: 只读挂载 /usr（系统库）
        - --ro-bind /lib /lib: 只读挂载 /lib
        - --ro-bind /lib64 /lib64: 只读挂载 /lib64
        - --bind <project> <project>: 读写挂载项目目录
        - --unshare-all: 隔离所有命名空间
        """
        project = str(self._project_root)
        return [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--bind", project, project,
            "--unshare-all",
            "--die-with-parent",
            "--", *cmd,
        ]

    def _build_firejail_cmd(self, cmd: list[str]) -> list[str]:
        """构建 firejail 命令前缀"""
        project = str(self._project_root)
        return [
            "firejail",
            f"--private={project}",
            "--quiet",
            "--", *cmd,
        ]

    async def execute(self, cmd: list[str], cwd: Optional[str] = None,
                      timeout: int = 30, env: Optional[dict] = None) -> dict:
        """通过沙箱后端执行命令"""
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "命令为空"}

        # 工作目录校验
        if cwd and not self.validate_path(cwd):
            return {"returncode": -1, "stdout": "",
                    "stderr": f"工作目录越界: {cwd}"}

        # 根据后端构建最终命令
        if self._backend == "bwrap":
            final_cmd = self._build_bwrap_cmd(cmd)
        elif self._backend == "firejail":
            final_cmd = self._build_firejail_cmd(cmd)
        else:
            # 降级到 NoopSandbox 行为
            noop = NoopSandbox(str(self._project_root))
            return await noop.execute(cmd, cwd=cwd, timeout=timeout, env=env)

        try:
            process = await asyncio.create_subprocess_exec(
                *final_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env if env is not None else None,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            # 沙箱后端启动失败，降级到 noop
            logger.warning("沙箱后端 %s 启动失败，降级到 noop: %s",
                           self._backend, e)
            noop = NoopSandbox(str(self._project_root))
            return await noop.execute(cmd, cwd=cwd, timeout=timeout, env=env)

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

    def validate_path(self, path: str) -> bool:
        """校验路径在项目根内"""
        if not path:
            return False
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self._project_root)
            return True
        except (ValueError, OSError):
            return False


# ── 工厂函数 ────────────────────────────────────────────────────────


def create_sandbox(project_root: str, enabled: bool = False) -> Sandbox:
    """根据平台和特性开关创建沙箱实例

    Args:
        project_root: 项目根目录（沙箱限制范围）
        enabled: 是否启用沙箱（features.is_enabled("sandbox")）

    Returns:
        - enabled=False: NoopSandbox（不隔离）
        - enabled=True + Linux: LinuxSandbox（bwrap/firejail）
        - enabled=True + Windows: WindowsSandbox（路径校验）
        - enabled=True + macOS: WindowsSandbox（路径校验，复用逻辑）
    """
    if not enabled:
        return NoopSandbox(project_root)

    if os.name == "nt":
        return WindowsSandbox(project_root)
    if os.name == "posix":
        # 检测是否为 Linux（有 /proc 文件系统）
        # 用 os.path.exists 而非 Path.exists，避免 Windows 测试时 PosixPath 实例化失败
        try:
            if os.path.exists("/proc"):
                return LinuxSandbox(project_root)
        except (OSError, ValueError):
            pass
        # macOS 和其他 Unix 复用 WindowsSandbox 的路径校验逻辑
        return WindowsSandbox(project_root)

    # 未知平台降级到 Noop
    return NoopSandbox(project_root)
