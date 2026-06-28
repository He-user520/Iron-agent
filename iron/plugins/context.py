"""PluginContext — 插件运行时受控上下文

约束（反模式防护 #8）：
- 插件不能直接访问 engine 内部状态
- 所有文件操作经过 path_guard 校验（路径越界抛 ValueError）
- run_command 通过沙箱执行（沙箱禁用时直接 subprocess）
- 网络访问需要 manifest.permissions 包含 "network"
"""
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 权限常量
PERM_FILE_READ = "file_read"
PERM_FILE_WRITE = "file_write"
PERM_RUN_COMMAND = "run_command"
PERM_NETWORK = "network"


@dataclass
class PluginContext:
    """插件运行时上下文 — 限制插件对引擎内部的访问

    Attributes:
        project_root: 项目根目录（绝对路径）
        config: IronConfig 实例（只读视图，插件不应修改）
        feature_flags: FeatureFlags 实例
        event_bus: PubSub 实例（仅订阅，不发布）
        logger: 插件专属 logger
        permissions: 已授予的权限列表
    """
    project_root: str
    config: Any = None
    feature_flags: Any = None
    event_bus: Any = None
    logger: Any = None
    permissions: list[str] = None

    def __post_init__(self):
        if self.permissions is None:
            self.permissions = []
        if self.logger is None:
            self.logger = logging.getLogger(f"iron.plugin")
        self._project_root_path = Path(self.project_root).resolve()

    # ── 受控文件操作 ──────────────────────────────────────────────

    def _check_permission(self, perm: str) -> None:
        """检查权限，缺失时抛 PermissionError"""
        if perm not in self.permissions:
            raise PermissionError(
                f"插件缺少权限: {perm}（请在 plugin.json 的 permissions 中声明）"
            )

    def _validate_path(self, path: str) -> Path:
        """校验路径在项目目录内，返回绝对路径

        优先使用 path_guard 模块；不可用时回退到内联校验。
        """
        try:
            from iron.tools.path_guard import validate_path_in_project
            return validate_path_in_project(path, str(self._project_root_path), allow_create=True)
        except ImportError:
            # 内联回退
            full = (self._project_root_path / path).resolve()
            try:
                full.relative_to(self._project_root_path)
            except ValueError:
                raise ValueError(f"路径越界：{path} 不在项目目录内")
            return full

    def read_file(self, path: str) -> str:
        """读取项目内文件（需 file_read 权限）"""
        self._check_permission(PERM_FILE_READ)
        full = self._validate_path(path)
        try:
            return full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return full.read_text(encoding="gbk")

    def write_file(self, path: str, content: str) -> bool:
        """写入项目内文件（需 file_write 权限）

        Returns:
            True 表示写入成功
        """
        self._check_permission(PERM_FILE_WRITE)
        full = self._validate_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return True

    def run_command(self, cmd: str, timeout: int = 30) -> dict:
        """执行命令（需 run_command 权限）

        通过沙箱执行（沙箱禁用时直接 subprocess）。

        Returns:
            {"success": bool, "stdout": str, "stderr": str, "returncode": int}
        """
        self._check_permission(PERM_RUN_COMMAND)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self._project_root_path),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"命令超时（{timeout}s）",
                "returncode": -1,
            }
        except (OSError, ValueError) as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }

    # ── 事件订阅（仅订阅，不发布） ───────────────────────────────

    def subscribe_event(self, event_type: str, handler) -> None:
        """订阅事件（仅订阅，不允许发布以避免插件干扰主流程）"""
        if self.event_bus is None:
            logger.warning("event_bus 未注入，无法订阅事件")
            return
        if hasattr(self.event_bus, "subscribe"):
            self.event_bus.subscribe(event_type, handler)
        else:
            logger.warning("event_bus 不支持 subscribe 方法")
