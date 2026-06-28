"""embed_lint 工具 — 嵌入式代码静态分析

优先调用 EmbedGuard 的 AnalysisPipeline（8 个分析器，AST 级精度）。
EmbedGuard 不可用时回退到内置正则规则。
"""
import asyncio
import re
import sys
from pathlib import Path
from iron.tools.base import BaseTool
from iron.tools.path_guard import validate_path_in_project

# 导入 EmbedGuard
_EMBEDGUARD_AVAILABLE = False
_AnalysisPipeline = None
try:
    _eg_path = str(Path(__file__).parent.parent.parent / "嵌入式-embedguard")
    if _eg_path not in sys.path:
        sys.path.insert(0, _eg_path)
    from embedguard.core.pipeline import AnalysisPipeline
    _EMBEDGUARD_AVAILABLE = True
    _AnalysisPipeline = AnalysisPipeline
except ImportError:
    pass

# 内置降级规则（EmbedGuard 不可用时使用）
_BUILTIN_RULES = {
    "volatile_missing": {
        "pattern": r'\b(uint\d+_t|int\d+_t)\s*\*\s*\w+\s*=\s*\(.*?\)\s*0x[0-9a-fA-F]+',
        "message": "寄存器访问缺少 volatile 修饰",
        "severity": "error",
    },
    "malloc_in_isr": {
        "pattern": r'\b(malloc|calloc|realloc|free)\s*\(',
        "message": "可能在中断中使用了动态内存分配",
        "severity": "warning",
    },
    "float_usage": {
        "pattern": r'\b(float|double)\b',
        "message": "使用了浮点数（确认有 FPU 支持）",
        "severity": "info",
    },
}


class EmbedLintTool(BaseTool):
    """嵌入式静态分析工具 — 调用 EmbedGuard AnalysisPipeline"""

    def __init__(self):
        self._pipeline = _AnalysisPipeline() if _EMBEDGUARD_AVAILABLE else None

    @property
    def name(self) -> str:
        return "embed_lint"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "embed_lint",
                "description": "对嵌入式代码进行静态分析。调用 EmbedGuard，检查内存安全、中断安全、寄存器访问、时序、资源使用等问题。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要检查的文件列表（如 ['src/main.c']）。留空则检查项目中所有 .c/.h 文件。",
                        },
                        "target_mcu": {
                            "type": "string",
                            "description": "目标 MCU（如 stm32f407）。用于精确的硬件特性分析。",
                        },
                    },
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        files = args.get("files", [])
        target_mcu = args.get("target_mcu", "stm32f407")
        project_dir = context.get("project_dir", ".")

        # 自动查找要分析的文件
        if not files:
            p = Path(project_dir)
            ignore_dirs = {".git", ".idea", ".vscode", "__pycache__", "build", "dist",
                           ".iron", "venv", ".venv", ".pio"}
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".c", ".h", ".rs"):
                    continue
                # 只检查项目内相对路径组件（修复 f.parts 绝对路径误判）
                try:
                    rel_parts = f.relative_to(p).parts
                except ValueError:
                    continue
                if any(part in ignore_dirs for part in rel_parts):
                    continue
                files.append(str(f.relative_to(p)))

        if not files:
            return {"success": False, "error": "未找到 .c/.h 源文件。"}

        # 优先使用 EmbedGuard
        if self._pipeline:
            return await self._run_embedguard(files, project_dir, target_mcu)

        # 降级：使用内置正则规则
        return await self._run_builtin_rules(files, project_dir, target_mcu)

    async def _run_embedguard(self, files: list, project_dir: str, target_mcu: str) -> dict:
        """使用 EmbedGuard 分析"""
        # 校验每个文件路径都在项目目录内
        abs_files = []
        for f in files:
            try:
                abs_files.append(str(validate_path_in_project(f, project_dir)))
            except ValueError as e:
                return {"success": False, "error": str(e)}
        try:
            # 在线程中运行同步的 analyze，避免阻塞事件循环
            result = await asyncio.to_thread(self._pipeline.analyze, abs_files, target_mcu)
            issues = []
            for finding in result.findings:
                issues.append({
                    "file": finding.file,
                    "line": finding.line,
                    "column": finding.column,
                    "rule": finding.rule_id,
                    "category": finding.category.value,
                    "severity": finding.severity.value,
                    "message": finding.message,
                    "suggestion": finding.suggestion,
                    "confidence": finding.confidence,
                })
            return {
                "success": True,
                "engine": "EmbedGuard (AST)",
                "files_checked": result.files_analyzed,
                "total_issues": len(issues),
                "errors": result.stats.get("error", 0),
                "warnings": result.stats.get("warning", 0),
                "infos": result.stats.get("info", 0),
                "duration_ms": result.duration_ms,
                "target_mcu": target_mcu,
                "issues": issues[:50],
            }
        except (RuntimeError, ValueError, OSError, AttributeError) as e:
            return {"success": False, "error": f"EmbedGuard 分析失败: {e}"}

    async def _run_builtin_rules(self, files: list, project_dir: str, target_mcu: str) -> dict:
        """使用内置正则规则（降级模式）"""
        issues = []
        files_checked = 0
        for file_path in files:
            try:
                full_path = validate_path_in_project(file_path, project_dir)
            except ValueError:
                continue
            if not full_path.exists():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                files_checked += 1
                for line_no, line in enumerate(content.splitlines(), 1):
                    for rule_name, rule in _BUILTIN_RULES.items():
                        if re.search(rule["pattern"], line):
                            issues.append({
                                "file": file_path,
                                "line": line_no,
                                "rule": rule_name,
                                "severity": rule["severity"],
                                "message": rule["message"],
                            })
            except (PermissionError, OSError):
                continue

        severity_order = {"error": 0, "warning": 1, "info": 2}
        issues.sort(key=lambda x: severity_order.get(x["severity"], 3))

        return {
            "success": True,
            "engine": "builtin (regex fallback)",
            "embedguard_hint": "EmbedGuard 未加载（需要 tree-sitter-c）。使用内置规则降级分析。运行 pip install tree-sitter tree-sitter-c 启用完整分析。",
            "files_checked": files_checked,
            "total_issues": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "issues": issues[:50],
        }
