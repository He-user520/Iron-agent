"""search_code 工具 — 代码内容搜索（基于 re 模块，兼容 Windows）"""
import os
import re
from pathlib import Path
from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

# 忽略的目录
IGNORE_DIRS = {".git", ".idea", ".vscode", "__pycache__", "node_modules",
               "build", "dist", ".cache", ".trae-cn", ".iron", "venv", ".venv"}


class SearchCodeTool(BaseTool):
    """搜索代码内容 — 在项目中搜索正则表达式匹配"""

    @property
    def name(self) -> str:
        return "search_code"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": "在项目中搜索代码内容（支持正则表达式）。用于查找函数定义、变量引用、错误信息等。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "搜索的正则表达式（如 'def main'、'TODO|FIXME'、'#include'）"},
                        "glob": {"type": "string", "description": "文件类型过滤（如 '*.c'、'*.py'、'*.h'），不填则搜索所有文件"},
                        "path": {"type": "string", "description": "搜索目录（默认 '.'）"},
                        "max_results": {"type": "integer", "description": "最大结果数（默认 30）"},
                    },
                    "required": ["pattern"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        pattern = args.get("pattern", "")
        glob_filter = args.get("glob", "")
        search_path = args.get("path", ".")
        max_results = args.get("max_results", 30)

        if not pattern:
            return {"success": False, "error": "缺少 pattern 参数"}

        # ReDoS 防护：限制正则表达式长度
        if len(pattern) > 500:
            return {"success": False, "error": "正则表达式过长（>500 字符），可能引发 ReDoS"}

        project_dir = context.get("project_dir", ".")
        try:
            root = validate_path_in_project(search_path, project_dir)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not root.exists():
            return {"success": False, "error": f"目录不存在: {search_path}"}

        # 编译正则
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"success": False, "error": f"正则表达式无效: {e}"}

        # 确定文件扩展名过滤
        ext_filter = None
        if glob_filter:
            if glob_filter.startswith("*."):
                ext_filter = glob_filter[1:].lower()  # ".c"
            elif glob_filter.startswith("."):
                ext_filter = glob_filter.lower()

        results = []
        files_searched = 0

        try:
            for item in sorted(root.rglob("*")):
                if len(results) >= max_results:
                    break
                if not item.is_file():
                    continue
                # 跳过忽略目录（用相对路径组件判断，避免匹配项目目录外的同名目录）
                try:
                    rel_parts = item.relative_to(root).parts
                except ValueError:
                    continue
                if any(part in IGNORE_DIRS for part in rel_parts):
                    continue
                # 扩展名过滤
                if ext_filter and item.suffix.lower() != ext_filter:
                    continue
                # 跳过二进制文件
                if item.suffix.lower() in {".exe", ".dll", ".so", ".o", ".a", ".hex", ".bin", ".elf", ".pyc", ".pyo"}:
                    continue

                files_searched += 1
                try:
                    text = item.read_text(encoding="utf-8", errors="ignore")
                    for line_no, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            rel = item.relative_to(root)
                            results.append({
                                "file": str(rel).replace("\\", "/"),
                                "line": line_no,
                                "text": line.strip()[:200],
                            })
                            if len(results) >= max_results:
                                break
                except (PermissionError, OSError):
                    continue
        except OSError as e:
            return {"success": False, "error": f"搜索失败: {e}"}

        return {
            "success": True,
            "pattern": pattern,
            "matches": len(results),
            "files_searched": files_searched,
            "results": results,
        }
