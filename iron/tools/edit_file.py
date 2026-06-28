"""edit_file 工具 — 精确文本替换编辑（参考 OpenCode edit.ts）"""
import logging
from pathlib import Path
from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

logger = logging.getLogger(__name__)


class EditFileTool(BaseTool):
    """精确编辑文件 — 用 old_string → new_string 替换，比全文件覆盖更安全"""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "精确编辑文件中的指定文本（old_string 必须完全匹配）。比 write_file 更安全，只修改需要改的部分。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件相对路径"},
                        "old_string": {"type": "string", "description": "要替换的精确文本（必须完全匹配原文，包括空格和缩进）"},
                        "new_string": {"type": "string", "description": "替换后的文本"},
                        "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false，只替换第一个）"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)

        if not path:
            return {"success": False, "error": "缺少 path 参数"}
        if not old_string:
            return {"success": False, "error": "缺少 old_string 参数。使用 write_file 创建或覆盖文件。"}
        if old_string == new_string:
            return {"success": False, "error": "old_string 和 new_string 相同，无需修改"}

        project_dir = context.get("project_dir", ".")
        try:
            full_path = validate_path_in_project(path, project_dir)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not full_path.exists():
            return {"success": False, "error": f"文件不存在: {path}"}

        # 大文件保护：超过阈值拒绝全量读取
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        try:
            if full_path.stat().st_size > MAX_FILE_SIZE:
                return {"success": False, "error": f"文件过大（>{MAX_FILE_SIZE // 1024 // 1024}MB），请用 search_code 局部查看"}
        except OSError as e:
            return {"success": False, "error": f"获取文件信息失败: {e}"}

        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"success": False, "error": f"读取文件失败: {e}"}

        # 统计匹配次数
        count = content.count(old_string)
        if count == 0:
            return {
                "success": False,
                "error": "old_string 在文件中未找到匹配。必须完全匹配，包括空格和缩进。请先用 read_file 查看文件内容。",
            }
        if count > 1 and not replace_all:
            return {
                "success": False,
                "error": f"找到 {count} 处匹配。请提供更多上下文使 old_string 唯一，或设置 replace_all=true。",
            }

        # 执行替换
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replacements = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replacements = 1

        # v4.0 Track 6: Diff 预览（写入前展示变更，让用户知情决策）
        # console 通过 context 传入（engine 注入），不直接 import 避免循环依赖
        # 特性门控：features.diff_preview 控制开关，失败不阻塞编辑
        console = context.get("console")
        if console is not None:
            try:
                from iron.config.features import is_feature_enabled
                if is_feature_enabled("diff_preview"):
                    from iron.cli.ui import _render_diff
                    _render_diff(console, content, new_content,
                                 file_path=path)
            except (ImportError, RuntimeError, ValueError) as _e:
                logger.debug("diff 预览渲染失败: %s", _e)

        # 写入文件
        try:
            full_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return {"success": False, "error": f"写入文件失败: {e}"}

        return {
            "success": True,
            "path": path,
            "replacements": replacements,
            "preview_old": old_string[:200],
            "preview_new": new_string[:200],
        }
