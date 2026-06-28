"""embed_build 工具 — 嵌入式项目编译（调用 EmbedForge）

优先使用 EmbedForge 的 EmbedForgeBuildServer（支持 PlatformIO/CMake/ESP-IDF/Keil/GCC）。
EmbedForge 不可用时回退到原生命令。
"""
import sys
from pathlib import Path
from iron.tools.base import BaseTool

# 尝试导入 EmbedForge
_EMBEDFORGE_AVAILABLE = False
_EmbedForgeBuildServer = None
try:
    # 将 EmbedForge 加入路径
    _ef_path = str(Path(__file__).parent.parent.parent / "嵌入式-EmbedForge")
    if _ef_path not in sys.path:
        sys.path.insert(0, _ef_path)
    from embedforge.servers.build_server.server import EmbedForgeBuildServer
    _EMBEDFORGE_AVAILABLE = True
    _EmbedForgeBuildServer = EmbedForgeBuildServer
except ImportError:
    pass


class EmbedBuildTool(BaseTool):
    """嵌入式编译工具 — 调用 EmbedForge 编译服务"""

    def __init__(self):
        self._ef_server = _EmbedForgeBuildServer() if _EMBEDFORGE_AVAILABLE else None

    @property
    def name(self) -> str:
        return "embed_build"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "embed_build",
                "description": "编译嵌入式项目。自动检测构建系统（PlatformIO/CMake/Make/ESP-IDF/Keil/GCC），返回编译结果和固件路径。需要用户授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["compile", "clean", "info", "scaffold"],
                            "description": "操作类型（默认 compile）",
                        },
                        "target": {
                            "type": "string",
                            "description": "PlatformIO 编译目标环境名（可选）",
                        },
                    },
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        action = args.get("action", "compile")
        target = args.get("target")
        project_dir = context.get("project_dir", ".")

        if not self._ef_server:
            return {
                "success": False,
                "error": "EmbedForge 未安装。请确认 嵌入式-EmbedForge 目录存在且依赖已安装（pip install -e 嵌入式-EmbedForge）。",
                "embedforge_available": False,
            }

        # 编译前先检查项目是否有构建系统
        if action == "compile":
            try:
                info = await self._ef_server.call_tool("get_build_info", {"project_dir": project_dir})
            except (RuntimeError, ValueError, OSError) as e:
                return {"success": False, "error": f"获取构建信息失败: {e}"}

            if not isinstance(info, dict):
                return {"success": False, "error": "获取构建信息返回非字典"}

            result_data = info.get("result") if info.get("success") else None
            build_system = result_data.get("build_system") if isinstance(result_data, dict) else None

            if not build_system or build_system == "gcc_bare":
                # 没有构建系统，检查是否是嵌入式代码
                p = Path(project_dir)
                has_hal = any(p.rglob("*.c")) and not (p / "platformio.ini").exists()
                if has_hal and not (p / "Makefile").exists() and not (p / "CMakeLists.txt").exists():
                    return {
                        "success": False,
                        "error": "项目缺少构建系统文件。检测到 .c 源文件但没有 Makefile/CMakeLists.txt/platformio.ini。",
                        "suggestion": "使用 embed_build action=scaffold 初始化 PlatformIO 项目（自动下载工具链），或手动创建 Makefile/CMakeLists.txt。",
                        "build_system": build_system,
                    }

        try:
            if action == "scaffold":
                return await self._scaffold_platformio(project_dir, context)
            elif action == "info":
                result = await self._ef_server.call_tool("get_build_info", {"project_dir": project_dir})
            elif action == "clean":
                result = await self._ef_server.call_tool("clean", {"project_dir": project_dir})
            else:
                result = await self._ef_server.call_tool("compile", {
                    "project_dir": project_dir,
                    "target": target,
                })
            # 统一包装结果，确保始终包含 success 字段
            return {"success": result.get("success", True), **result}
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": f"EmbedForge 编译失败: {e}"}

    async def _scaffold_platformio(self, project_dir: str, context: dict) -> dict:
        """初始化 PlatformIO 项目结构"""
        from embedforge.templates import get_template

        p = Path(project_dir)

        # 检测 MCU 类型（从源文件推断或使用默认值）
        mcu = "stm32f407"
        for c_file in p.glob("*.c"):
            try:
                content = c_file.read_text(encoding="utf-8", errors="ignore")
                if "stm32f1" in content.lower():
                    mcu = "stm32f103"
                elif "stm32f4" in content.lower():
                    mcu = "stm32f407"
                elif "stm32l4" in content.lower():
                    mcu = "stm32l476"
                elif "esp32" in content.lower():
                    mcu = "esp32"
            except (OSError, UnicodeDecodeError) as e:
                import logging
                logging.warning(f"读取源文件 {c_file.name} 检测 MCU 失败: {e}")

        # 使用 EmbedForge 的 STM32 裸机模板
        try:
            template = get_template("stm32_baremetal")
            if template:
                template.apply(p, {"mcu": mcu})
                return {"success": True, "message": f"已初始化 PlatformIO 项目（{mcu}）", "mcu": mcu}
        except (RuntimeError, OSError, ValueError) as e:
            import logging
            logging.warning(f"EmbedForge 模板应用失败，回退到手动创建: {e}")

        # 手动创建 platformio.ini
        board_map = {
            "stm32f407": "genericSTM32F407VGT6",
            "stm32f103": "genericSTM32F103C8",
            "stm32l476": "genericSTM32L476RG",
            "esp32": "esp32dev",
        }
        board = board_map.get(mcu, "genericSTM32F407VGT6")

        ini_content = f"""[env:{mcu}]
platform = ststm32
board = {board}
framework = stm32cube
build_flags = -DUSE_HAL_DRIVER
monitor_speed = 115200
"""
        if mcu == "esp32":
            ini_content = f"""[env:esp32]
platform = espressif32
board = esp32dev
framework = arduino
monitor_speed = 115200
"""

        # 复制源文件到 src/ 目录（非破坏性：保留原文件，便于回滚）
        import shutil as _shutil
        src_dir = p / "src"
        src_dir.mkdir(exist_ok=True)
        for c_file in list(p.glob("*.c")):
            target = src_dir / c_file.name
            if not target.exists():
                _shutil.copy2(c_file, target)

        (p / "platformio.ini").write_text(ini_content, encoding="utf-8")

        return {
            "success": True,
            "message": f"已初始化 PlatformIO 项目（{mcu}, board={board}）",
            "mcu": mcu,
            "board": board,
            "platformio_ini": ini_content,
        }
