"""Patch 工具 — 应用 unified diff 补丁

参考 Claude Code 的 Edit 工具设计：
- 接受 unified diff 格式
- 支持多文件补丁
- 支持模糊匹配（行尾空白差异）
- 失败时返回详细错误信息
"""
import re
from pathlib import Path

from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project


class PatchTool(BaseTool):
    """应用 unified diff 补丁修改文件

    用法:
        patch = PatchTool()
        result = await patch.execute({
            "diff": "--- a/src/main.c\\n+++ b/src/main.c\\n@@ -10,3 +10,4 @@\\n...",
        }, {"project_dir": "."})
    """

    # 文件大小上限（10MB），防止读取超大文件导致内存问题
    MAX_FILE_SIZE = 10 * 1024 * 1024

    @property
    def name(self) -> str:
        return "patch"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "patch",
                "description": "应用 unified diff 补丁修改文件。支持多文件补丁，比 edit_file 更适合批量修改。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "diff": {
                            "type": "string",
                            "description": "unified diff 格式的补丁内容",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "只检查不实际应用，默认 false",
                        },
                    },
                    "required": ["diff"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        """执行补丁应用

        Args:
            args: 包含 diff（必填）和 dry_run（可选）的参数字典
            context: 上下文，包含 project_dir 等

        Returns:
            结果 dict，包含 success/applied/failed/files 等字段
        """
        diff = args.get("diff", "")
        dry_run = args.get("dry_run", False)

        if not diff or not diff.strip():
            return {"success": False, "error": "diff 内容为空"}

        project_dir = context.get("project_dir", ".")

        # 1. 解析 diff 为多个文件补丁
        patches = self._parse_diff(diff)
        if not patches:
            return {"success": False, "error": "无法解析 diff 内容"}

        # 2. 应用每个文件补丁
        results = []
        for patch in patches:
            if dry_run:
                result = self._verify_patch(patch, project_dir)
            else:
                result = self._apply_patch(patch, project_dir)
            results.append(result)

        # 3. 汇总
        success_count = sum(1 for r in results if r["success"])
        failed_count = len(results) - success_count

        return {
            "success": failed_count == 0,
            "applied": success_count,
            "failed": failed_count,
            "files": results,
            "dry_run": dry_run,
        }

    def _parse_diff(self, diff: str) -> list[dict]:
        """解析 unified diff 为多个文件补丁

        返回 [{"file": "src/main.c", "hunks": [{"old_start": 10, "lines": [...]}]}]

        支持 git diff（--- a/file, +++ b/file）和标准 unified diff 格式。
        """
        patches = []
        current_file = None
        current_hunks: list[dict] = []
        in_hunk = False  # 标记是否正在读取 hunk 内容

        # 用 splitlines() 而非 split("\n")：避免 diff 末尾换行产生伪空行
        # （空行会被当作 context 行处理，导致多出一条假的 context）
        for line in diff.splitlines():
            if line.startswith("--- "):
                # 源文件头 — 等待 +++ 行确定目标文件
                in_hunk = False
                continue
            elif line.startswith("+++ "):
                # 目标文件头
                file_path = line[4:].strip()
                # 去除 b/ 前缀（git diff 风格）
                if file_path.startswith("b/"):
                    file_path = file_path[2:]
                # 去除可能的 \t 后缀（如 "b/file\t2023-01-01"）
                file_path = file_path.split("\t")[0].strip()
                # 收集上一个文件
                if current_file is not None:
                    patches.append({"file": current_file, "hunks": current_hunks})
                current_file = file_path
                current_hunks = []
                in_hunk = False
            elif line.startswith("@@"):
                # hunk 头 — 形如 @@ -10,3 +10,4 @@ context
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if m:
                    current_hunks.append({
                        "old_start": int(m.group(1)),
                        "old_count": int(m.group(2) or 1),
                        "new_start": int(m.group(3)),
                        "new_count": int(m.group(4) or 1),
                        "lines": [],
                    })
                    in_hunk = True
                else:
                    in_hunk = False
            elif in_hunk and current_hunks:
                # hunk 内容行
                if not line:
                    # 空行视为 context 行（unified diff 规范：空行就是 context）
                    current_hunks[-1]["lines"].append(("context", ""))
                elif line.startswith("+"):
                    current_hunks[-1]["lines"].append(("add", line[1:]))
                elif line.startswith("-"):
                    current_hunks[-1]["lines"].append(("remove", line[1:]))
                elif line.startswith(" "):
                    current_hunks[-1]["lines"].append(("context", line[1:]))
                elif line.startswith("\\"):
                    # \ No newline at end of file — 忽略
                    pass
                else:
                    # 其他行（如新的 diff --git 头）— 视为 hunk 结束
                    in_hunk = False
            elif line.startswith("diff --git") or line.startswith("Index:"):
                # 文件分隔符 — 不影响当前文件（直到 +++ 出现）
                in_hunk = False

        # 收集最后一个文件
        if current_file is not None:
            patches.append({"file": current_file, "hunks": current_hunks})

        return patches

    def _read_file(self, full_path: Path) -> str:
        """读取文件内容，支持 UTF-8 和 GBK fallback

        优先 UTF-8，失败 fallback 到 GBK，再失败用 errors="replace"
        """
        # 大文件保护
        try:
            size = full_path.stat().st_size
        except OSError as e:
            raise OSError(f"获取文件信息失败: {e}")
        if size > self.MAX_FILE_SIZE:
            raise OSError(f"文件过大（>{self.MAX_FILE_SIZE // 1024 // 1024}MB）")

        try:
            return full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return full_path.read_text(encoding="gbk")
            except UnicodeDecodeError:
                return full_path.read_text(encoding="utf-8", errors="replace")

    def _resolve_path(self, file_path: str, project_dir: str) -> Path:
        """解析文件路径并做边界校验

        允许文件不存在（allow_create=True），由 _verify_patch 检查文件存在性
        """
        return validate_path_in_project(file_path, project_dir, allow_create=True)

    def _verify_patch(self, patch: dict, project_dir: str) -> dict:
        """验证补丁是否能应用（不实际修改文件）

        检查项：
        - 文件存在
        - 行号在范围内
        - remove/context 行匹配（模糊匹配，容忍行尾空白差异）
        """
        file_path = patch["file"]
        try:
            full_path = self._resolve_path(file_path, project_dir)
        except (ValueError, FileNotFoundError) as e:
            return {"success": False, "file": file_path, "error": str(e)}

        if not full_path.exists():
            return {"success": False, "file": file_path, "error": "文件不存在"}

        try:
            content = self._read_file(full_path)
        except OSError as e:
            return {"success": False, "file": file_path, "error": str(e)}

        lines = content.split("\n")

        for hunk_idx, hunk in enumerate(patch["hunks"]):
            start_idx = hunk["old_start"] - 1
            if start_idx < 0:
                start_idx = 0
            if start_idx > len(lines):
                return {
                    "success": False, "file": file_path,
                    "error": f"hunk {hunk_idx + 1}: 行号超出范围 (start={hunk['old_start']}, file_lines={len(lines)})",
                }

            idx = start_idx
            for op, text in hunk["lines"]:
                if op in ("remove", "context"):
                    if idx >= len(lines):
                        return {
                            "success": False, "file": file_path,
                            "error": f"hunk {hunk_idx + 1}: 行 {idx + 1} 超出文件范围",
                        }
                    # 模糊匹配：行尾空白差异容忍（rstrip 比较）
                    actual = lines[idx].rstrip()
                    expected = text.rstrip()
                    if actual != expected:
                        return {
                            "success": False, "file": file_path,
                            "error": f"hunk {hunk_idx + 1}: 行 {idx + 1} 不匹配: 期望 '{expected[:50]}', 实际 '{actual[:50]}'",
                        }
                    idx += 1

        return {"success": True, "file": file_path, "hunks": len(patch["hunks"])}

    def _apply_patch(self, patch: dict, project_dir: str) -> dict:
        """应用补丁（先验证，再实际修改文件）

        从后往前应用 hunk（按 old_start 降序），避免行号偏移。
        """
        file_path = patch["file"]
        try:
            full_path = self._resolve_path(file_path, project_dir)
        except (ValueError, FileNotFoundError) as e:
            return {"success": False, "file": file_path, "error": str(e)}

        # 先验证
        verify = self._verify_patch(patch, project_dir)
        if not verify["success"]:
            return verify

        try:
            content = self._read_file(full_path)
        except OSError as e:
            return {"success": False, "file": file_path, "error": str(e)}

        lines = content.split("\n")

        # 从后往前应用 hunk（避免行号偏移）
        sorted_hunks = sorted(patch["hunks"], key=lambda h: h["old_start"], reverse=True)

        for hunk in sorted_hunks:
            start_idx = max(0, hunk["old_start"] - 1)
            old_count = hunk["old_count"]
            constructed_new: list[str] = []
            walk_idx = start_idx
            for op, text in hunk["lines"]:
                if op == "context":
                    # 保留原行（保持原始格式），越界时用 diff 中的 text
                    if walk_idx < len(lines):
                        constructed_new.append(lines[walk_idx])
                    else:
                        constructed_new.append(text)
                    walk_idx += 1
                elif op == "add":
                    constructed_new.append(text)
                elif op == "remove":
                    walk_idx += 1  # 跳过旧行
            # 替换 [start_idx, start_idx + old_count) 为 constructed_new
            end_idx = start_idx + old_count
            if end_idx > len(lines):
                end_idx = len(lines)
            lines[start_idx:end_idx] = constructed_new

        # 写入文件（统一 UTF-8 编码）
        new_content = "\n".join(lines)
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return {"success": False, "file": file_path, "error": f"写入文件失败: {e}"}

        return {"success": True, "file": file_path, "hunks": len(patch["hunks"])}
