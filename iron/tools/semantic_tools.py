"""语义工具 — 暴露给 Agent 使用的代码索引查询

4 个只读工具：
- semantic_search: 按关键词搜索符号（"HAL_Delay"）
- get_callers: 查找函数调用者
- get_callees: 查找函数被调用者
- find_dead_code: 查找未被调用的函数（死代码）

工具通过 context["code_indexer"] 获取索引器实例。
索引器不可用时返回 success=False + 空结果，不崩溃主流程。
"""
import logging

from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _get_indexer(context: dict):
    """从 context 中获取 CodeIndexer 实例，不可用时返回 None"""
    indexer = context.get("code_indexer")
    if indexer is None or not getattr(indexer, "available", False):
        return None
    return indexer


class SemanticSearchTool(BaseTool):
    """按关键词搜索符号"""

    @property
    def name(self) -> str:
        return "semantic_search"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "semantic_search",
                "description": (
                    "按名称搜索代码符号（函数/变量/类型/宏）。"
                    "基于 tree-sitter 代码索引，支持模糊匹配。"
                    "适合查找 'HAL_Delay' 'UART_Init' 等符号的定义位置。"
                    "无需授权，只读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词（符号名片段）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回结果上限（默认 20）",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        indexer = _get_indexer(context)
        if indexer is None:
            return {
                "success": False,
                "error": "代码索引未启用（安装 tree_sitter tree_sitter_c 并启用 code_indexer 特性）",
                "matches": [],
            }
        query = args.get("query", "").strip()
        if not query:
            return {"success": False, "error": "缺少 query 参数", "matches": []}
        limit = args.get("limit", 20)
        try:
            results = indexer.search_symbols(query, limit=limit)
            return {
                "success": True,
                "query": query,
                "count": len(results),
                "matches": [
                    {
                        "name": r["name"],
                        "kind": r["kind"],
                        "file": r["file_path"],
                        "line_start": r["line_start"],
                        "line_end": r["line_end"],
                    }
                    for r in results
                ],
            }
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "matches": []}


class GetCallersTool(BaseTool):
    """查找函数调用者"""

    @property
    def name(self) -> str:
        return "get_callers"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "get_callers",
                "description": (
                    "查找调用指定函数的所有位置（谁调用了这个函数）。"
                    "基于代码索引的调用图（callgraph）。"
                    "例如查询谁调用了 HAL_Delay。无需授权，只读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function": {
                            "type": "string",
                            "description": "要查询被调用的函数名（如 HAL_Delay）",
                        },
                    },
                    "required": ["function"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        indexer = _get_indexer(context)
        if indexer is None:
            return {
                "success": False,
                "error": "代码索引未启用",
                "callers": [],
            }
        func_name = args.get("function", "").strip()
        if not func_name:
            return {"success": False, "error": "缺少 function 参数", "callers": []}
        try:
            results = indexer.get_callers(func_name)
            return {
                "success": True,
                "function": func_name,
                "count": len(results),
                "callers": [
                    {
                        "caller": r["caller_name"],
                        "file": r["caller_file"],
                        "line": r["caller_line"],
                    }
                    for r in results
                ],
            }
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "callers": []}


class GetCalleesTool(BaseTool):
    """查找函数被调用者"""

    @property
    def name(self) -> str:
        return "get_callees"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "get_callees",
                "description": (
                    "查找指定函数调用的所有函数（这个函数调用了谁）。"
                    "基于代码索引的调用图（callgraph）。"
                    "例如查询 main 函数内部调用了哪些函数。无需授权，只读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function": {
                            "type": "string",
                            "description": "要查询的函数名（如 main）",
                        },
                    },
                    "required": ["function"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        indexer = _get_indexer(context)
        if indexer is None:
            return {
                "success": False,
                "error": "代码索引未启用",
                "callees": [],
            }
        func_name = args.get("function", "").strip()
        if not func_name:
            return {"success": False, "error": "缺少 function 参数", "callees": []}
        try:
            results = indexer.get_callees(func_name)
            return {
                "success": True,
                "function": func_name,
                "count": len(results),
                "callees": [
                    {
                        "callee": r["callee_name"],
                        "file": r["caller_file"],
                        "line": r["caller_line"],
                    }
                    for r in results
                ],
            }
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "callees": []}


class FindDeadCodeTool(BaseTool):
    """查找未被调用的函数（死代码）"""

    @property
    def name(self) -> str:
        return "find_dead_code"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "find_dead_code",
                "description": (
                    "查找项目中未被任何函数调用的函数定义（死代码）。"
                    "基于代码索引的调用图分析，帮助清理冗余代码。"
                    "注意：入口函数（main, ISR handler）可能被识别为死代码，需人工判断。"
                    "无需授权，只读。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        indexer = _get_indexer(context)
        if indexer is None:
            return {
                "success": False,
                "error": "代码索引未启用",
                "dead_code": [],
            }
        try:
            results = indexer.find_dead_code()
            return {
                "success": True,
                "count": len(results),
                "dead_code": [
                    {
                        "name": r["name"],
                        "file": r["file_path"],
                        "line_start": r["line_start"],
                        "line_end": r["line_end"],
                    }
                    for r in results
                ],
            }
        except (RuntimeError, ValueError, OSError) as e:
            return {"success": False, "error": str(e), "dead_code": []}


def register_semantic_tools(registry, indexer=None) -> None:
    """注册所有语义工具到 ToolRegistry

    Args:
        registry: ToolRegistry 实例
        indexer: CodeIndexer 实例（可选，用于初始化工具）
    """
    tools = [
        SemanticSearchTool(),
        GetCallersTool(),
        GetCalleesTool(),
        FindDeadCodeTool(),
    ]
    for tool in tools:
        registry.register(tool)
    # indexer 通过 context 注入，无需在工具构造时传入
