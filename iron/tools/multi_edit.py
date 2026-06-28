"""MultiEdit — 多文件原子编辑工具（v4.0 Track 7）

设计原则：
- 原子性：所有编辑成功才提交，任一失败回滚所有已编辑文件
- 复用 diff 预览：每个文件的编辑都调用 Track 6 的 _render_diff
- 权限回调：与 edit_file 同级，requires_permission=True
- 限制数量：单次最多 10 个文件（防止误操作）
- 特性门控：features.multi_edit 控制开关

工具调用示例：
    {
        "edits": [
            {"path": "src/a.c", "old_string": "foo", "new_string": "bar"},
            {"path": "src/b.c", "old_string": "baz", "new_string": "qux"},
        ]
    }
"""
import logging
from pathlib import Path

from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

logger = logging.getLogger(__name__)

MAX_FILES = 10  # 单次最多编辑 10 个文件


class MultiEditTool(BaseTool):
    """multi_edit — 多文件原子编辑"""

    @property
    def name(self) -> str:
        return "multi_edit"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "multi_edit",
                "description": (
                    "原子编辑多个文件（要么全成功，要么全回滚）。"
                    "单次最多 10 个文件。每个编辑项需指定 path/old_string/new_string。"
                    "需要用户确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "文件相对路径"},
                                    "old_string": {"type": "string", "description": "要替换的精确文本（必须完全匹配）"},
                                    "new_string": {"type": "string", "description": "替换后的文本"},
                                },
                                "required": ["path", "old_string", "new_string"],
                            },
                            "description": "编辑列表（最多 10 个）",
                        },
                    },
                    "required": ["edits"],
                },
            },
        }

    @property
    def requires_permission(self) -> bool:
        """与 edit_file 同级，需要用户确认"""
        return True

    async def execute(self, args: dict, context: dict) -> dict:
        edits = args.get("edits", [])
        if not edits:
            return {"success": False, "error": "edits 不能为空",
                    "output": ""}
        if len(edits) > MAX_FILES:
            return {"success": False,
                    "error": f"单次最多编辑 {MAX_FILES} 个文件（当前 {len(edits)} 个）",
                    "output": ""}

        project_dir = context.get("project_dir") or context.get("project_root") or "."
        console = context.get("console")

        # 阶段 1：预检查 + 备份 + diff 预览
        # 所有文件先读取原内容备份，并校验 old_string 能匹配
        # 任何一项失败 → 回滚已备份的文件（此时还没写入，回滚即恢复原状）
        backups = []  # [(full_path, original_content, encoding)]
        for idx, edit in enumerate(edits):
            path = edit.get("path", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")

            if not path:
                self._rollback(backups)
                return {"success": False, "error": f"第 {idx + 1} 项缺少 path",
                        "output": ""}
            if not old_string:
                self._rollback(backups)
                return {"success": False, "error": f"{path}: 缺少 old_string",
                        "output": ""}
            if old_string == new_string:
                self._rollback(backups)
                return {"success": False, "error": f"{path}: old_string 和 new_string 相同",
                        "output": ""}

            # 路径校验（防越界）
            # validate_path_in_project 在 strict=True 模式下对不存在文件抛 FileNotFoundError
            try:
                full_path = validate_path_in_project(path, project_dir)
            except ValueError as e:
                self._rollback(backups)
                return {"success": False, "error": f"{path}: {e}",
                        "output": ""}
            except FileNotFoundError:
                self._rollback(backups)
                return {"success": False, "error": f"文件不存在: {path}",
                        "output": ""}

            if not full_path.exists():
                self._rollback(backups)
                return {"success": False, "error": f"文件不存在: {path}",
                        "output": ""}

            # 大文件保护
            MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
            try:
                if full_path.stat().st_size > MAX_FILE_SIZE:
                    self._rollback(backups)
                    return {"success": False,
                            "error": f"{path}: 文件过大（>{MAX_FILE_SIZE // 1024 // 1024}MB）",
                            "output": ""}
            except OSError as e:
                self._rollback(backups)
                return {"success": False, "error": f"{path}: 获取文件信息失败: {e}",
                        "output": ""}

            # 读取原内容（UTF-8 优先，GBK 回退）
            encoding = "utf-8"
            try:
                original = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    original = full_path.read_text(encoding="gbk", errors="replace")
                    encoding = "gbk"
                except OSError as e:
                    self._rollback(backups)
                    return {"success": False, "error": f"{path}: 读取失败: {e}",
                            "output": ""}
            except OSError as e:
                self._rollback(backups)
                return {"success": False, "error": f"{path}: 读取失败: {e}",
                        "output": ""}

            # 匹配检查
            if old_string not in original:
                self._rollback(backups)
                return {"success": False,
                        "error": f"{path}: 未找到匹配内容（old_string 必须完全匹配）",
                        "output": ""}

            # diff 预览（特性门控 + console 注入）
            if console is not None:
                try:
                    from iron.config.features import is_feature_enabled
                    if is_feature_enabled("diff_preview"):
                        from iron.cli.ui import _render_diff
                        new_content = original.replace(old_string, new_string, 1)
                        _render_diff(console, original, new_content,
                                     file_path=path)
                except (ImportError, RuntimeError, ValueError) as _e:
                    logger.debug("diff 预览失败 %s: %s", path, _e)

            backups.append((full_path, original, encoding))

        # 阶段 2：原子执行（所有预检查通过后才开始写入）
        # 任何一次写入失败 → 回滚所有已写入的文件
        written = []  # 已成功写入的 (full_path, original, encoding)
        results = []
        for edit, (full_path, original, encoding) in zip(edits, backups):
            path = edit["path"]
            old_string = edit["old_string"]
            new_string = edit["new_string"]
            try:
                new_content = original.replace(old_string, new_string, 1)
                full_path.write_text(new_content, encoding="utf-8")
                written.append((full_path, original, encoding))
                results.append(f"✓ {path}")
            except OSError as e:
                # 写入失败 → 回滚所有已写入的文件
                self._rollback(written)
                return {"success": False,
                        "error": f"写入 {path} 失败: {e}，已回滚所有变更",
                        "output": ""}

        return {
            "success": True,
            "output": f"已原子编辑 {len(results)} 个文件:\n" + "\n".join(results),
            "error": None,
            "files_modified": [e["path"] for e in edits],
        }

    def _rollback(self, backups: list) -> None:
        """回滚已备份的文件（用原内容覆盖）

        Args:
            backups: [(full_path, original_content, encoding), ...]
        """
        for full_path, original, encoding in backups:
            try:
                # 统一用 utf-8 写回（简化编码处理，原内容已是字符串）
                full_path.write_text(original, encoding="utf-8")
            except OSError as e:
                logger.error("回滚 %s 失败: %s", full_path, e)


def register_multi_edit_tool(registry) -> None:
    """注册 MultiEdit 工具到 registry"""
    registry.register(MultiEditTool())
