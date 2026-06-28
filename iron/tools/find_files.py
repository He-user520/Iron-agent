"""find_files 工具 — 文件查找（基于 glob 模式匹配）"""
from pathlib import Path
from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

IGNORE_DIRS = {".git", ".idea", ".vscode", "__pycache__", "node_modules",
               "build", "dist", ".cache", ".trae-cn", ".iron", "venv", ".venv"}


class FindFilesTool(BaseTool):
    """按模式查找文件"""

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "find_files",
                "description": "按 glob 模式查找项目中的文件。用于查找特定类型的文件（如 **/*.h、src/**/*.c）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "glob 模式（如 '**/*.c'、'src/**/*.h'、'*.py'）"},
                        "path": {"type": "string", "description": "搜索根目录（默认 '.'）"},
                        "max_results": {"type": "integer", "description": "最大结果数（默认 50）"},
                    },
                    "required": ["pattern"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        pattern = args.get("pattern", "")
        search_path = args.get("path", ".")
        max_results = args.get("max_results", 50)

        if not pattern:
            return {"success": False, "error": "缺少 pattern 参数"}

        project_dir = context.get("project_dir", ".")
        try:
            root = validate_path_in_project(search_path, project_dir)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not root.exists():
            return {"success": False, "error": f"目录不存在: {search_path}"}

        results = []
        try:
            for item in sorted(root.glob(pattern)):
                if len(results) >= max_results:
                    break
                if not item.is_file():
                    continue
                if any(part in IGNORE_DIRS for part in item.relative_to(root).parts):
                    continue
                rel = item.relative_to(root)
                results.append({
                    "path": str(rel).replace("\\", "/"),
                    "size": item.stat().st_size,
                })
        except OSError as e:
            return {"success": False, "error": f"查找失败: {e}"}

        return {
            "success": True,
            "pattern": pattern,
            "count": len(results),
            "files": results,
        }
