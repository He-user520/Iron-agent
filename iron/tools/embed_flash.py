"""embed_flash 工具 — 嵌入式固件烧录（调用 EmbedForge）

调用 EmbedForge 的 EmbedForgeHardwareServer.flash_firmware，
支持 ST-Link / J-Link / CMSIS-DAP / PlatformIO。
"""
import sys
from pathlib import Path
from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

# 导入 EmbedForge hardware_server
_EMBEDFORGE_HW_AVAILABLE = False
_EmbedForgeHardwareServer = None
try:
    _ef_path = str(Path(__file__).parent.parent.parent / "嵌入式-EmbedForge")
    if _ef_path not in sys.path:
        sys.path.insert(0, _ef_path)
    from embedforge.servers.hardware_server.server import EmbedForgeHardwareServer
    _EMBEDFORGE_HW_AVAILABLE = True
    _EmbedForgeHardwareServer = EmbedForgeHardwareServer
except ImportError:
    pass


class EmbedFlashTool(BaseTool):
    """嵌入式烧录工具 — 调用 EmbedForge hardware_server"""

    def __init__(self):
        self._hw_server = _EmbedForgeHardwareServer() if _EMBEDFORGE_HW_AVAILABLE else None

    @property
    def name(self) -> str:
        return "embed_flash"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "embed_flash",
                "description": "烧录固件到目标芯片。调用 EmbedForge 烧录服务，支持 ST-Link / J-Link / CMSIS-DAP。需要用户授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "firmware": {
                            "type": "string",
                            "description": "固件路径（如 .pio/build/stm32f407/firmware.bin）。留空则自动查找。",
                        },
                        "probe": {
                            "type": "string",
                            "enum": ["auto", "stlink", "jlink", "cmsis_dap"],
                            "description": "调试探针类型（默认 auto 自动检测）",
                        },
                        "target": {
                            "type": "string",
                            "description": "目标芯片型号（如 stm32f4x）。留空则自动检测。",
                        },
                    },
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        firmware = args.get("firmware", "")
        probe = args.get("probe", "auto")
        target = args.get("target")
        project_dir = context.get("project_dir", ".")

        if not self._hw_server:
            return {
                "success": False,
                "error": "EmbedForge hardware_server 未加载。请确认 嵌入式-EmbedForge 目录存在。",
            }

        # 自动查找固件
        if not firmware:
            firmware = self._find_firmware(project_dir)
            if not firmware:
                return {
                    "success": False,
                    "error": "未找到固件文件。请先用 embed_build 编译项目，或指定 firmware 路径。",
                }

        # 校验固件路径在项目目录内
        try:
            firmware_resolved = validate_path_in_project(firmware, project_dir)
        except (ValueError, FileNotFoundError) as e:
            return {"success": False, "error": f"固件路径无效或不存在: {e}"}
        firmware_path = str(firmware_resolved)

        try:
            result = await self._hw_server.call_tool("flash_firmware", {
                "firmware_path": firmware_path,
                "probe": probe,
                "target": target,
                "verify": True,
                "reset_after": True,
            })
            return result
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": f"EmbedForge 烧录失败: {e}"}

    def _find_firmware(self, project_dir: str) -> str:
        """自动查找编译产物"""
        p = Path(project_dir)
        # PlatformIO 标准路径
        for ext in (".bin", ".hex", ".elf", ".uf2"):
            for f in sorted(p.rglob(f"*{ext}")):
                # 检查项目内相对路径组件（避免匹配路径中包含 ".pio"/"build" 子串的误判）
                try:
                    rel_parts = f.relative_to(p).parts
                except ValueError:
                    continue
                if ".pio" in rel_parts or "build" in rel_parts:
                    return str(f.relative_to(p))
        return ""
