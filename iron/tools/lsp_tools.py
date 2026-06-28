"""LSP 工具 — 暴露给 Agent 使用

提供：
- lsp_diagnostics: 获取 C/C++ 文件的编译诊断信息
- lsp_definition: 跳转定义
- lsp_references: 查找引用
- lsp_hover: 悬停文档
- lsp_completion: 代码补全

LSP 服务器不可用时优雅降级（返回 success=False + 空列表），
所有 async 方法处理 asyncio.CancelledError。
"""
import asyncio
import logging
from typing import Optional

from iron.integrations.lsp_client import LSPClient
from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _severity_name(severity: int) -> str:
    """LSP severity 数字转名称"""
    return {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(severity, "unknown")


class LSPDiagnosticsTool(BaseTool):
    """获取文件诊断信息"""

    def __init__(self, client: Optional[LSPClient] = None):
        super().__init__()
        self._client = client

    def set_client(self, client: LSPClient) -> None:
        """注入 LSP 客户端"""
        self._client = client

    @property
    def name(self) -> str:
        return "lsp_diagnostics"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "lsp_diagnostics",
                "description": "获取 C/C++ 文件的编译诊断信息（错误、警告）。基于 LSP（clangd/ccls）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "文件路径（如 'src/main.c'）",
                        },
                    },
                    "required": ["file"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        if not self._client or not getattr(self._client, "_initialized", False):
            return {"success": False, "error": "LSP 服务器未启动", "diagnostics": []}
        file = args.get("file", "")
        if not file:
            return {"success": False, "error": "缺少 file 参数", "diagnostics": []}
        try:
            diags = await self._client.get_diagnostics(file)
            return {
                "success": True,
                "file": file,
                "count": len(diags),
                "diagnostics": [
                    {
                        "file": d.file,
                        "line": d.line,
                        "col": d.col,
                        "end_line": d.end_line,
                        "end_col": d.end_col,
                        "severity": d.severity,
                        "severity_name": _severity_name(d.severity),
                        "source": d.source,
                        "message": d.message,
                        "code": d.code,
                    }
                    for d in diags
                ],
            }
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "diagnostics": []}


class LSPDefinitionTool(BaseTool):
    """跳转定义"""

    def __init__(self, client: Optional[LSPClient] = None):
        super().__init__()
        self._client = client

    def set_client(self, client: LSPClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "lsp_definition"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "lsp_definition",
                "description": "跳转到符号定义位置（C/C++）。基于 LSP（clangd/ccls）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径"},
                        "line": {"type": "integer", "description": "行号（0-based）"},
                        "col": {"type": "integer", "description": "列号（0-based）"},
                    },
                    "required": ["file", "line", "col"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        if not self._client or not getattr(self._client, "_initialized", False):
            return {"success": False, "error": "LSP 服务器未启动", "definitions": []}
        file = args.get("file", "")
        line = args.get("line", 0)
        col = args.get("col", 0)
        if not file:
            return {"success": False, "error": "缺少 file 参数", "definitions": []}
        try:
            positions = await self._client.definition(file, line, col)
            return {
                "success": True,
                "file": file,
                "line": line,
                "col": col,
                "count": len(positions),
                "definitions": [
                    {"file": p.file, "line": p.line, "col": p.col}
                    for p in positions
                ],
            }
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "definitions": []}


class LSPReferencesTool(BaseTool):
    """查找引用"""

    def __init__(self, client: Optional[LSPClient] = None):
        super().__init__()
        self._client = client

    def set_client(self, client: LSPClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "lsp_references"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "lsp_references",
                "description": "查找符号的所有引用位置（C/C++）。基于 LSP（clangd/ccls）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径"},
                        "line": {"type": "integer", "description": "行号（0-based）"},
                        "col": {"type": "integer", "description": "列号（0-based）"},
                    },
                    "required": ["file", "line", "col"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        if not self._client or not getattr(self._client, "_initialized", False):
            return {"success": False, "error": "LSP 服务器未启动", "references": []}
        file = args.get("file", "")
        line = args.get("line", 0)
        col = args.get("col", 0)
        if not file:
            return {"success": False, "error": "缺少 file 参数", "references": []}
        try:
            positions = await self._client.references(file, line, col)
            return {
                "success": True,
                "file": file,
                "line": line,
                "col": col,
                "count": len(positions),
                "references": [
                    {"file": p.file, "line": p.line, "col": p.col}
                    for p in positions
                ],
            }
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "references": []}


class LSPHoverTool(BaseTool):
    """悬停文档"""

    def __init__(self, client: Optional[LSPClient] = None):
        super().__init__()
        self._client = client

    def set_client(self, client: LSPClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "lsp_hover"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "lsp_hover",
                "description": "获取符号悬停文档（C/C++ 类型、函数签名、注释）。基于 LSP（clangd/ccls）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径"},
                        "line": {"type": "integer", "description": "行号（0-based）"},
                        "col": {"type": "integer", "description": "列号（0-based）"},
                    },
                    "required": ["file", "line", "col"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        if not self._client or not getattr(self._client, "_initialized", False):
            return {"success": False, "error": "LSP 服务器未启动", "hover": None}
        file = args.get("file", "")
        line = args.get("line", 0)
        col = args.get("col", 0)
        if not file:
            return {"success": False, "error": "缺少 file 参数", "hover": None}
        try:
            hover = await self._client.hover(file, line, col)
            if hover is None:
                return {"success": True, "file": file, "line": line, "col": col, "hover": None}
            return {
                "success": True,
                "file": file,
                "line": line,
                "col": col,
                "hover": {
                    "content": hover.content,
                    "range_start": (
                        {
                            "file": hover.range_start.file,
                            "line": hover.range_start.line,
                            "col": hover.range_start.col,
                        }
                        if hover.range_start else None
                    ),
                    "range_end": (
                        {
                            "file": hover.range_end.file,
                            "line": hover.range_end.line,
                            "col": hover.range_end.col,
                        }
                        if hover.range_end else None
                    ),
                },
            }
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "hover": None}


class LSPCompletionTool(BaseTool):
    """代码补全"""

    def __init__(self, client: Optional[LSPClient] = None):
        super().__init__()
        self._client = client

    def set_client(self, client: LSPClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "lsp_completion"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "lsp_completion",
                "description": "获取代码补全建议（C/C++）。基于 LSP（clangd/ccls）。无需授权。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "文件路径"},
                        "line": {"type": "integer", "description": "行号（0-based）"},
                        "col": {"type": "integer", "description": "列号（0-based）"},
                    },
                    "required": ["file", "line", "col"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        if not self._client or not getattr(self._client, "_initialized", False):
            return {"success": False, "error": "LSP 服务器未启动", "completions": []}
        file = args.get("file", "")
        line = args.get("line", 0)
        col = args.get("col", 0)
        if not file:
            return {"success": False, "error": "缺少 file 参数", "completions": []}
        try:
            completions = await self._client.completion(file, line, col)
            return {
                "success": True,
                "file": file,
                "line": line,
                "col": col,
                "count": len(completions),
                "completions": [
                    {
                        "label": c.label,
                        "kind": c.kind,
                        "detail": c.detail,
                        "documentation": c.documentation,
                        "insert_text": c.insert_text,
                    }
                    for c in completions
                ],
            }
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "completions": []}
