"""Windows 符号链接路径穿越测试（Phase 2 任务 2.4）

专门验证 Windows 平台下符号链接路径穿越防护：
- symlink 指向项目外文件 → 被拦截
- symlink 指向项目外目录 → 被拦截
- 断链 symlink → 被拦截（OSError/FileNotFoundError）
- allow_create 模式下 symlink 安全
- 跨平台行为一致性（Linux 也应拦截）
- Windows 保留设备名（CON/PRN/AUX/NUL）拦截
- Path.resolve(strict=True) 在 Windows 上的行为

运行方式: pytest tests/test_windows_symlink.py -v
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

import pytest

from iron.tools.path_guard import validate_path_in_project, _WIN_RESERVED_NAMES


# ── 辅助函数 ─────────────────────────────────────────────────────

def _can_create_symlink() -> bool:
    """检测当前环境是否可以创建符号链接

    Windows 上需要管理员权限或开发者模式；Linux/macOS 通常都可以。
    即使首次尝试成功，后续也可能因 Windows 激活上下文问题失败，
    因此这里返回的结论只是"可能可用"，具体测试中仍需 try/except。
    """
    if sys.platform != "win32":
        return True
    # Windows：尝试创建一个临时 symlink 检测权限
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.txt"
            src.write_text("test")
            link = Path(tmp) / "link.txt"
            link.symlink_to(src)
            return True
    except (OSError, NotImplementedError):
        return False


def _try_symlink(link: Path, target, target_is_directory: bool = False) -> bool:
    """尝试创建 symlink，失败时返回 False（用于测试中优雅降级）"""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
        return True
    except (OSError, NotImplementedError):
        return False


# 装饰器：无法创建 symlink 时跳过测试
skip_if_no_symlink = pytest.mark.skipif(
    not _can_create_symlink(),
    reason="当前环境无法创建符号链接（Windows 需要管理员权限或开发者模式）",
)


def _safe_cleanup(path: Path, is_dir: bool = False) -> None:
    """安全清理路径（symlink/文件/目录），忽略错误"""
    try:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink():
            if sys.platform == "win32" and is_dir:
                os.rmdir(str(path))
            else:
                path.unlink()
        elif is_dir:
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        pass


# ── 符号链接穿越测试 ─────────────────────────────────────────────

class TestSymlinkTraversal:
    """符号链接指向项目外的拦截测试"""

    def setup_method(self):
        self.project_dir = str(Path(tempfile.mkdtemp()).resolve())

    def teardown_method(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    @skip_if_no_symlink
    def test_symlink_to_outside_file_blocked(self, tmp_path):
        """symlink 指向项目外文件 → 被拦截"""
        outside_dir = Path(tempfile.mkdtemp())
        outside = outside_dir / "secret.txt"
        outside.write_text("secret")
        link_path = Path(self.project_dir) / "link.txt"
        if not _try_symlink(link_path, outside):
            pytest.skip("无法创建符号链接")
        try:
            with pytest.raises((ValueError, FileNotFoundError)):
                validate_path_in_project("link.txt", self.project_dir, allow_create=False)
        finally:
            _safe_cleanup(link_path)
            _safe_cleanup(outside)
            shutil.rmtree(outside_dir, ignore_errors=True)

    @skip_if_no_symlink
    def test_symlink_to_outside_dir_blocked(self, tmp_path):
        """symlink 指向项目外目录 → 被拦截"""
        outside_dir = Path(tempfile.mkdtemp())
        (outside_dir / "data.txt").write_text("data")
        link_dir = Path(self.project_dir) / "ext_link"
        if not _try_symlink(link_dir, outside_dir, target_is_directory=True):
            pytest.skip("无法创建符号链接")
        try:
            with pytest.raises((ValueError, FileNotFoundError)):
                validate_path_in_project(
                    "ext_link/data.txt", self.project_dir, allow_create=False
                )
        finally:
            _safe_cleanup(link_dir, is_dir=True)
            shutil.rmtree(outside_dir, ignore_errors=True)

    @skip_if_no_symlink
    def test_symlink_to_inside_file_allowed(self):
        """symlink 指向项目内文件 → 允许"""
        real_file = Path(self.project_dir) / "real.txt"
        real_file.write_text("content")
        link = Path(self.project_dir) / "link.txt"
        if not _try_symlink(link, real_file):
            pytest.skip("无法创建符号链接")
        try:
            result = validate_path_in_project(
                "link.txt", self.project_dir, allow_create=False
            )
            assert str(result).startswith(self.project_dir)
        finally:
            _safe_cleanup(link)

    @skip_if_no_symlink
    def test_broken_symlink_blocked(self):
        """断链 symlink（目标不存在）→ 被拦截"""
        link = Path(self.project_dir) / "broken.txt"
        if not _try_symlink(link, Path(self.project_dir) / "nonexistent.txt"):
            pytest.skip("无法创建符号链接")
        try:
            with pytest.raises((ValueError, FileNotFoundError, OSError)):
                validate_path_in_project(
                    "broken.txt", self.project_dir, allow_create=False
                )
        finally:
            _safe_cleanup(link)

    @skip_if_no_symlink
    def test_symlink_chain_to_outside_blocked(self):
        """多层 symlink 链最终指向项目外 → 被拦截"""
        outside_dir = Path(tempfile.mkdtemp())
        outside = outside_dir / "target.txt"
        outside.write_text("secret")
        link1 = Path(self.project_dir) / "link1.txt"
        link2 = Path(self.project_dir) / "link2.txt"
        if not _try_symlink(link2, outside):
            pytest.skip("无法创建符号链接")
        if not _try_symlink(link1, link2):
            pytest.skip("无法创建符号链接")
        try:
            with pytest.raises((ValueError, FileNotFoundError)):
                validate_path_in_project(
                    "link1.txt", self.project_dir, allow_create=False
                )
        finally:
            _safe_cleanup(link1)
            _safe_cleanup(link2)
            _safe_cleanup(outside)
            shutil.rmtree(outside_dir, ignore_errors=True)

    @skip_if_no_symlink
    def test_allow_create_with_symlink_in_project(self):
        """allow_create=True 时项目内 symlink 仍可访问"""
        real = Path(self.project_dir) / "real.txt"
        real.write_text("content")
        link = Path(self.project_dir) / "link.txt"
        if not _try_symlink(link, real):
            pytest.skip("无法创建符号链接")
        try:
            result = validate_path_in_project(
                "link.txt", self.project_dir, allow_create=True
            )
            assert str(result).startswith(self.project_dir)
        finally:
            _safe_cleanup(link)


# ── Windows 保留设备名测试 ───────────────────────────────────────

class TestWindowsReservedNames:
    """Windows 保留设备名测试（CON/PRN/AUX/NUL/COM1-9/LPT1-9）"""

    def setup_method(self):
        self.project_dir = str(Path(tempfile.mkdtemp()).resolve())

    @pytest.mark.parametrize("name", ["CON", "PRN", "AUX", "NUL"])
    def test_reserved_name_blocked(self, name):
        """Windows 保留设备名被拦截"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project(
                f"src/{name}.txt", self.project_dir, allow_create=True
            )

    @pytest.mark.parametrize("i", range(1, 10))
    def test_com_port_blocked(self, i):
        """COM1-9 被拦截"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project(
                f"src/COM{i}.txt", self.project_dir, allow_create=True
            )

    @pytest.mark.parametrize("i", range(1, 10))
    def test_lpt_port_blocked(self, i):
        """LPT1-9 被拦截"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project(
                f"src/LPT{i}.txt", self.project_dir, allow_create=True
            )

    def test_reserved_name_with_extension(self):
        """CON.txt 这种带扩展名的形式也被拦截"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project(
                "src/CON.log", self.project_dir, allow_create=True
            )

    def test_reserved_name_case_insensitive(self):
        """小写 con 也被拦截（大小写不敏感）"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project(
                "src/con.txt", self.project_dir, allow_create=True
            )

    def test_normal_name_not_blocked(self):
        """普通文件名不触发保留设备名检查"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        result = validate_path_in_project(
            "src/main.c", self.project_dir, allow_create=True
        )
        assert "main.c" in str(result)

    def test_reserved_set_complete(self):
        """_WIN_RESERVED_NAMES 包含所有保留名"""
        # 必须包含 CON/PRN/AUX/NUL
        assert "CON" in _WIN_RESERVED_NAMES
        assert "PRN" in _WIN_RESERVED_NAMES
        assert "AUX" in _WIN_RESERVED_NAMES
        assert "NUL" in _WIN_RESERVED_NAMES
        # COM1-9
        for i in range(1, 10):
            assert f"COM{i}" in _WIN_RESERVED_NAMES
        # LPT1-9
        for i in range(1, 10):
            assert f"LPT{i}" in _WIN_RESERVED_NAMES


# ── 路径解析行为测试 ─────────────────────────────────────────────

class TestPathResolveBehavior:
    """Path.resolve() 在不同平台的行为测试"""

    def setup_method(self):
        self.project_dir = str(Path(tempfile.mkdtemp()).resolve())

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def test_normal_relative_path_resolved(self):
        """正常相对路径解析到项目内"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        result = validate_path_in_project(
            "src/main.c", self.project_dir, allow_create=True
        )
        assert result.is_absolute()
        assert result.exists() is False  # allow_create 允许不存在

    def test_dotdot_traversal_blocked(self):
        """../ 穿越被拦截"""
        outside_dir = Path(self.project_dir).parent / "traversal_test_outside"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret")
        try:
            with pytest.raises(ValueError, match="路径越界"):
                validate_path_in_project(
                    f"../{outside_dir.name}/secret.txt",
                    self.project_dir,
                    allow_create=True,
                )
        finally:
            outside_file.unlink(missing_ok=True)
            try:
                outside_dir.rmdir()
            except OSError:
                shutil.rmtree(outside_dir, ignore_errors=True)

    def test_absolute_path_inside_project_allowed(self):
        """项目内绝对路径允许"""
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        abs_path = str(Path(self.project_dir) / "src" / "main.c")
        result = validate_path_in_project(abs_path, self.project_dir, allow_create=True)
        assert str(result).startswith(self.project_dir)

    def test_absolute_path_outside_blocked(self):
        """项目外绝对路径被拦截"""
        # /etc/passwd 在 Linux 存在 → ValueError；Windows 不存在 → FileNotFoundError
        # 两者都应被视为"被拦截"
        with pytest.raises((ValueError, FileNotFoundError)):
            validate_path_in_project("/etc/passwd", self.project_dir, allow_create=False)

    def test_project_root_cannot_be_fs_root(self):
        """project_dir 不能是文件系统根目录"""
        # Windows: C:\ 或 D:\
        # Linux: /
        if sys.platform == "win32":
            root = "C:\\"
        else:
            root = "/"
        with pytest.raises((ValueError, RuntimeError, OSError)):
            # 根目录 resolve 会抛错或在 relative_to 时抛错
            validate_path_in_project("test.txt", root, allow_create=True)

    def test_empty_path_blocked(self):
        """空路径被拦截"""
        with pytest.raises(ValueError, match="路径不能为空"):
            validate_path_in_project("", self.project_dir, allow_create=True)

    def test_whitespace_path_blocked(self):
        """纯空白路径被拦截"""
        with pytest.raises(ValueError, match="路径不能为空"):
            validate_path_in_project("   ", self.project_dir, allow_create=True)


# ── 跨平台一致性测试 ─────────────────────────────────────────────

class TestCrossPlatformConsistency:
    """跨平台行为一致性"""

    def test_reserved_names_blocked_on_all_platforms(self):
        """Windows 保留名在所有平台都拦截（避免跨平台行为不一致）"""
        project_dir = str(Path(tempfile.mkdtemp()).resolve())
        (Path(project_dir) / "src").mkdir(exist_ok=True)
        # 不论在 Windows 还是 Linux，CON 都应被拦截
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            validate_path_in_project("src/CON.txt", project_dir, allow_create=True)

    def test_path_separator_normalization(self):
        """正反斜杠在 Windows 上都能识别"""
        project_dir = str(Path(tempfile.mkdtemp()).resolve())
        (Path(project_dir) / "src").mkdir(exist_ok=True)
        # Windows 接受 / 和 \
        if sys.platform == "win32":
            # 正斜杠
            result1 = validate_path_in_project("src/main.c", project_dir, allow_create=True)
            # 反斜杠
            result2 = validate_path_in_project("src\\main.c", project_dir, allow_create=True)
            # 解析后应一致
            assert str(result1) == str(result2)
