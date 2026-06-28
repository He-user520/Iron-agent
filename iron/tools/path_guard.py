"""路径边界安全校验模块 — 防止路径穿越攻击"""
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Windows 保留设备名（即使是 Linux 也拒绝，避免跨平台行为不一致）
_WIN_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def validate_path_in_project(path: str, project_dir: str, *, allow_create: bool = False) -> Path:
    """校验文件路径位于项目目录内，返回解析后的绝对路径。

    防止路径穿越攻击（如 ../../etc/passwd）。
    使用 Path.resolve() + relative_to() 替代不安全的 str.startswith()。

    Args:
        path: 相对路径（相对于 project_dir）
        project_dir: 项目根目录
        allow_create: True 表示路径可以不存在（用于 write_file）

    Returns:
        解析后的绝对路径

    Raises:
        ValueError: 路径越界或为空
    """
    if not path or not str(path).strip():
        raise ValueError("路径不能为空")

    project_root = Path(project_dir).resolve(strict=True)

    # 防止 project_dir 是文件系统根目录
    if project_root == project_root.parent:
        raise ValueError("project_dir 不能是文件系统根目录")

    # 拼接：处理绝对路径
    raw = Path(path)
    if raw.is_absolute():
        candidate = raw
    else:
        candidate = project_root / raw

    if not allow_create:
        # strict=True：要求路径所有组件（含符号链接）真实存在
        candidate = candidate.resolve(strict=True)
    else:
        # allow_create 时路径可能未存在，先解析父目录
        try:
            parent_resolved = candidate.parent.resolve(strict=True)
            candidate = parent_resolved / candidate.name
            # 如果路径已存在，再次 strict 解析展开符号链接
            if candidate.exists():
                candidate = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise ValueError(f"父目录不存在: {candidate.parent}")
        except OSError as e:
            # 不降级到 strict=False，直接拒绝（符号链接解析失败可能意味着路径有问题，如断裂符号链接）
            raise ValueError(f"路径解析失败，可能存在断裂符号链接: {e}")

    # Windows 保留设备名检查（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含 "CON.txt" 这种带扩展名形式）
    # 即使在 Linux 也拒绝，避免跨平台行为不一致
    name_upper = candidate.name.upper().split(".")[0]
    if name_upper in _WIN_RESERVED_NAMES:
        raise ValueError(f"路径包含 Windows 保留设备名: {candidate.name}")

    # 边界校验
    try:
        candidate.relative_to(project_root)
    except ValueError:
        raise ValueError(f"路径越界：禁止访问项目目录外的文件: {path}")

    return candidate
