"""EmbedForge 桥接模块 — 调用 EmbedForge 编译/烧录/串口工具"""
import shutil
import subprocess
from pathlib import Path


def _detect_build_system(project_dir: str) -> str:
    """从项目文件检测构建系统类型（EmbedForge 不可用时的兜底）"""
    p = Path(project_dir)
    if (p / "platformio.ini").exists():
        return "platformio"
    if (p / "CMakeLists.txt").exists():
        return "cmake"
    if (p / "Makefile").exists():
        return "make"
    if (p / "MDK-ARM").exists() or list(p.glob("*.uvprojx")):
        return "keil"
    if (p / "build.ninja").exists():
        return "ninja"
    return ""


def _try_cli_build(project_dir: str, build_system: str) -> dict:
    """直接调用构建工具链 CLI 作为 EmbedForge 不可用时的兜底

    返回与 compile_project 相同的结构，tool 字段标识实际使用的工具。
    """
    p = Path(project_dir)
    if build_system == "platformio":
        if not shutil.which("pio"):
            return {
                "success": False,
                "output": "PlatformIO CLI (pio) 未安装",
                "error": "tool_missing",
                "tool": "platformio",
                "hint": "pip install platformio 或访问 https://platformio.org 安装",
            }
        try:
            proc = subprocess.run(
                ["pio", "run"], cwd=project_dir, capture_output=True, text=True, timeout=300,
            )
            return {
                "success": proc.returncode == 0,
                "output": proc.stdout + ("\n" + proc.stderr if proc.stderr else ""),
                "tool": "platformio",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "编译超时（>300s）", "error": "timeout", "tool": "platformio"}
        except (subprocess.SubprocessError, OSError) as e:
            return {"success": False, "output": str(e), "error": "run_error", "tool": "platformio"}

    if build_system == "make":
        if not shutil.which("make") and not shutil.which("mingw32-make"):
            return {
                "success": False,
                "output": "make 未安装",
                "error": "tool_missing",
                "tool": "make",
                "hint": "安装 MSYS2 或使用 Keil/PlatformIO 替代",
            }
        make_bin = "make" if shutil.which("make") else "mingw32-make"
        try:
            proc = subprocess.run(
                [make_bin], cwd=project_dir, capture_output=True, text=True, timeout=300,
            )
            return {"success": proc.returncode == 0, "output": proc.stdout + proc.stderr, "tool": "make"}
        except (subprocess.SubprocessError, OSError) as e:
            return {"success": False, "output": str(e), "error": "run_error", "tool": "make"}

    if build_system == "cmake":
        build_dir = p / "build"
        if not build_dir.exists():
            return {
                "success": False,
                "output": "未找到 build 目录，请先运行 cmake 配置",
                "error": "no_build_dir",
                "tool": "cmake",
                "hint": "mkdir build && cd build && cmake ..",
            }
        if not shutil.which("cmake"):
            return {
                "success": False,
                "output": "cmake 未安装",
                "error": "tool_missing",
                "tool": "cmake",
                "hint": "从 https://cmake.org 下载安装",
            }
        try:
            proc = subprocess.run(
                ["cmake", "--build", "build"], cwd=project_dir, capture_output=True, text=True, timeout=300,
            )
            return {"success": proc.returncode == 0, "output": proc.stdout + proc.stderr, "tool": "cmake"}
        except (subprocess.SubprocessError, OSError) as e:
            return {"success": False, "output": str(e), "error": "run_error", "tool": "cmake"}

    return {
        "success": False,
        "output": f"构建系统 {build_system} 暂不支持直接调用",
        "error": "unsupported",
        "tool": build_system,
    }


def compile_project(project_dir: str, target: str = "", clean: bool = False) -> dict:
    """编译嵌入式项目

    Args:
        project_dir: 项目目录
        target: 构建目标（如 "debug"/"release"）— 暂未实现，预留参数
        clean: 是否在编译前 clean — 暂未实现，预留参数

    Returns:
        {"success": True, "output": "...", "flash_usage": "12.3KB", "ram_usage": "1.8KB"}
        失败时返回 {"success": False, "output": "...", "error": "...", "hint": "...", "tool": "..."}
    """
    # 注意：target 和 clean 参数当前未传递给底层 BuildServer，
    # 待 EmbedForge 支持后再启用
    embedforge_available = True
    try:
        from embedforge.servers.build_server.base import detect_build_system
        from embedforge.servers.build_server.server import BuildServer
    except ImportError:
        embedforge_available = False

    if embedforge_available:
        try:
            build_system = detect_build_system(Path(project_dir))
            if not build_system:
                return {"success": False, "output": "未检测到构建系统", "error": "no_build_system"}

            server = BuildServer()
            result = server.compile(project_dir)
            return {
                "success": True,
                "output": result.get("output", ""),
                "flash_usage": result.get("flash_usage", ""),
                "ram_usage": result.get("ram_usage", ""),
                "tool": "embedforge",
            }
        except (RuntimeError, OSError, ValueError) as e:
            return {"success": False, "output": str(e), "error": "compile_error", "tool": "embedforge"}

    # P3 修复（第七轮）：EmbedForge 不可用时，尝试直接调用构建工具链
    # 给出可操作建议而不是只报"EmbedForge 未安装"
    build_system = _detect_build_system(project_dir)
    if not build_system:
        return {
            "success": False,
            "output": "未检测到构建系统（未找到 platformio.ini / CMakeLists.txt / Makefile）",
            "error": "no_build_system",
            "hint": "EmbedForge 未安装，且无法识别项目的构建系统。可运行 `pip install embedforge` 启用统一构建支持。",
        }

    # 尝试直接调用对应 CLI 工具
    result = _try_cli_build(project_dir, build_system)
    # 附加安装 EmbedForge 的建议（仅在工具缺失时）
    if not result.get("success") and result.get("error") == "tool_missing":
        result["hint"] = (result.get("hint", "") + "\n或安装 EmbedForge 获得统一构建支持: pip install embedforge").strip()
    return result


def flash_firmware(firmware_path: str, probe: str = "") -> dict:
    """烧录固件到 MCU

    Returns:
        {"success": True, "output": "..."}
    """
    try:
        from embedforge.servers.hardware_server.flash import FlashManager
    except ImportError:
        return {"success": False, "output": "EmbedForge 未安装", "error": "import_error"}

    try:
        manager = FlashManager()
        result = manager.flash(firmware_path, probe=probe)
        return {"success": True, "output": result.get("output", "")}
    except (RuntimeError, OSError, ValueError) as e:
        return {"success": False, "output": str(e), "error": "flash_error"}


def list_serial_ports() -> list[str]:
    """列出可用串口"""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]
    except ImportError:
        return []


def list_probes() -> list[str]:
    """列出已连接的调试探针"""
    try:
        from embedforge.servers.hardware_server.flash import FlashManager
        manager = FlashManager()
        return manager.list_probes()
    except (ImportError, RuntimeError, OSError, ValueError):
        return []
