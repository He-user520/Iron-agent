"""语义工具测试 — semantic_search / get_callers / get_callees / find_dead_code

验证：
- 工具 schema 格式正确
- code_indexer 不可用时优雅降级
- 工具正确委托给 indexer
- 异常处理不崩溃
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from iron.tools.semantic_tools import (
    SemanticSearchTool,
    GetCallersTool,
    GetCalleesTool,
    FindDeadCodeTool,
    register_semantic_tools,
    _get_indexer,
)
from iron.tools.base import BaseTool


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture
def event_loop():
    """每个测试独立的 event loop"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_indexer():
    """mock 的 CodeIndexer（available=True）"""
    indexer = MagicMock()
    indexer.available = True
    return indexer


@pytest.fixture
def unavailable_indexer():
    """mock 的 CodeIndexer（available=False，降级模式）"""
    indexer = MagicMock()
    indexer.available = False
    return indexer


@pytest.fixture
def ctx_with_indexer(mock_indexer):
    """带 code_indexer 的 context"""
    return {"project_dir": ".", "engine": None, "code_indexer": mock_indexer}


@pytest.fixture
def ctx_without_indexer():
    """不带 code_indexer 的 context"""
    return {"project_dir": ".", "engine": None}


# ── 1. SemanticSearchTool ───────────────────────────────────────

class TestSemanticSearchTool:
    """semantic_search 工具测试"""

    def test_name_property(self):
        tool = SemanticSearchTool()
        assert tool.name == "semantic_search"

    def test_schema_format(self):
        """schema 符合 OpenAI function calling 格式"""
        tool = SemanticSearchTool()
        schema = tool.schema
        assert schema["type"] == "function"
        assert "function" in schema
        assert schema["function"]["name"] == "semantic_search"
        assert "query" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["required"]

    def test_inherits_base_tool(self):
        """继承 BaseTool"""
        tool = SemanticSearchTool()
        assert isinstance(tool, BaseTool)

    @pytest.mark.asyncio
    async def test_execute_with_indexer(self, ctx_with_indexer, mock_indexer):
        """有 indexer 时返回搜索结果"""
        mock_indexer.search_symbols.return_value = [
            {"name": "HAL_Delay", "kind": "function",
             "file_path": "src/hal.c", "line_start": 10, "line_end": 15,
             "col_start": 0, "col_end": 5, "project_path": "/p",
             "indexed_at": "2026-01-01", "id": 1},
        ]
        tool = SemanticSearchTool()
        result = await tool.execute({"query": "HAL"}, ctx_with_indexer)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["matches"][0]["name"] == "HAL_Delay"
        mock_indexer.search_symbols.assert_called_once_with("HAL", limit=20)

    @pytest.mark.asyncio
    async def test_execute_without_indexer(self, ctx_without_indexer):
        """无 indexer 时降级返回 success=False"""
        tool = SemanticSearchTool()
        result = await tool.execute({"query": "HAL"}, ctx_without_indexer)
        assert result["success"] is False
        assert "未启用" in result["error"]
        assert result["matches"] == []

    @pytest.mark.asyncio
    async def test_execute_with_unavailable_indexer(self, unavailable_indexer):
        """indexer.available=False 时降级"""
        ctx = {"code_indexer": unavailable_indexer}
        tool = SemanticSearchTool()
        result = await tool.execute({"query": "HAL"}, ctx)
        assert result["success"] is False
        assert result["matches"] == []

    @pytest.mark.asyncio
    async def test_execute_empty_query(self, ctx_with_indexer):
        """空 query 返回错误"""
        tool = SemanticSearchTool()
        result = await tool.execute({"query": ""}, ctx_with_indexer)
        assert result["success"] is False
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_missing_query(self, ctx_with_indexer):
        """缺少 query 参数返回错误"""
        tool = SemanticSearchTool()
        result = await tool.execute({}, ctx_with_indexer)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_execute_custom_limit(self, ctx_with_indexer, mock_indexer):
        """自定义 limit 传递给 indexer"""
        mock_indexer.search_symbols.return_value = []
        tool = SemanticSearchTool()
        await tool.execute({"query": "test", "limit": 5}, ctx_with_indexer)
        mock_indexer.search_symbols.assert_called_once_with("test", limit=5)

    @pytest.mark.asyncio
    async def test_execute_handles_indexer_exception(self, ctx_with_indexer, mock_indexer):
        """indexer 抛异常时不崩溃"""
        mock_indexer.search_symbols.side_effect = RuntimeError("db error")
        tool = SemanticSearchTool()
        result = await tool.execute({"query": "test"}, ctx_with_indexer)
        assert result["success"] is False
        assert "db error" in result["error"]


# ── 2. GetCallersTool ───────────────────────────────────────────

class TestGetCallersTool:
    """get_callers 工具测试"""

    def test_name_property(self):
        assert GetCallersTool().name == "get_callers"

    def test_schema_required_function(self):
        schema = GetCallersTool().schema
        assert "function" in schema["function"]["parameters"]["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_callers(self, ctx_with_indexer, mock_indexer):
        mock_indexer.get_callers.return_value = [
            {"caller_name": "main", "caller_file": "src/main.c",
             "caller_line": 10, "callee_name": "HAL_Delay",
             "project_path": "/p", "indexed_at": "x", "id": 1},
        ]
        tool = GetCallersTool()
        result = await tool.execute({"function": "HAL_Delay"}, ctx_with_indexer)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["callers"][0]["caller"] == "main"

    @pytest.mark.asyncio
    async def test_execute_without_indexer(self, ctx_without_indexer):
        tool = GetCallersTool()
        result = await tool.execute({"function": "HAL_Delay"}, ctx_without_indexer)
        assert result["success"] is False
        assert result["callers"] == []

    @pytest.mark.asyncio
    async def test_execute_empty_function(self, ctx_with_indexer):
        tool = GetCallersTool()
        result = await tool.execute({"function": ""}, ctx_with_indexer)
        assert result["success"] is False
        assert "function" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self, ctx_with_indexer, mock_indexer):
        mock_indexer.get_callers.side_effect = ValueError("bad query")
        tool = GetCallersTool()
        result = await tool.execute({"function": "x"}, ctx_with_indexer)
        assert result["success"] is False
        assert "bad query" in result["error"]


# ── 3. GetCalleesTool ───────────────────────────────────────────

class TestGetCalleesTool:
    """get_callees 工具测试"""

    def test_name_property(self):
        assert GetCalleesTool().name == "get_callees"

    @pytest.mark.asyncio
    async def test_execute_returns_callees(self, ctx_with_indexer, mock_indexer):
        mock_indexer.get_callees.return_value = [
            {"caller_name": "main", "callee_name": "HAL_Init",
             "caller_file": "src/main.c", "caller_line": 10,
             "project_path": "/p", "indexed_at": "x", "id": 1},
        ]
        tool = GetCalleesTool()
        result = await tool.execute({"function": "main"}, ctx_with_indexer)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["callees"][0]["callee"] == "HAL_Init"

    @pytest.mark.asyncio
    async def test_execute_without_indexer(self, ctx_without_indexer):
        tool = GetCalleesTool()
        result = await tool.execute({"function": "main"}, ctx_without_indexer)
        assert result["success"] is False
        assert result["callees"] == []

    @pytest.mark.asyncio
    async def test_execute_empty_function(self, ctx_with_indexer):
        tool = GetCalleesTool()
        result = await tool.execute({"function": ""}, ctx_with_indexer)
        assert result["success"] is False


# ── 4. FindDeadCodeTool ─────────────────────────────────────────

class TestFindDeadCodeTool:
    """find_dead_code 工具测试"""

    def test_name_property(self):
        assert FindDeadCodeTool().name == "find_dead_code"

    def test_schema_no_required_params(self):
        """find_dead_code 不需要参数"""
        schema = FindDeadCodeTool().schema
        assert schema["function"]["parameters"]["properties"] == {}
        assert "required" not in schema["function"]["parameters"] or \
               schema["function"]["parameters"]["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_dead_code(self, ctx_with_indexer, mock_indexer):
        mock_indexer.find_dead_code.return_value = [
            {"name": "unused_func", "file_path": "src/util.c",
             "line_start": 1, "line_end": 3, "kind": "function",
             "project_path": "/p", "indexed_at": "x", "id": 1,
             "col_start": 0, "col_end": 1},
        ]
        tool = FindDeadCodeTool()
        result = await tool.execute({}, ctx_with_indexer)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["dead_code"][0]["name"] == "unused_func"

    @pytest.mark.asyncio
    async def test_execute_without_indexer(self, ctx_without_indexer):
        tool = FindDeadCodeTool()
        result = await tool.execute({}, ctx_without_indexer)
        assert result["success"] is False
        assert result["dead_code"] == []

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self, ctx_with_indexer, mock_indexer):
        mock_indexer.find_dead_code.side_effect = OSError("disk error")
        tool = FindDeadCodeTool()
        result = await tool.execute({}, ctx_with_indexer)
        assert result["success"] is False
        assert "disk error" in result["error"]


# ── 5. 辅助函数和注册函数 ───────────────────────────────────────

class TestHelpersAndRegistration:
    """_get_indexer 和 register_semantic_tools 测试"""

    def test_get_indexer_returns_none_when_missing(self):
        """context 中无 code_indexer 时返回 None"""
        assert _get_indexer({}) is None

    def test_get_indexer_returns_none_when_unavailable(self, unavailable_indexer):
        """indexer.available=False 时返回 None"""
        assert _get_indexer({"code_indexer": unavailable_indexer}) is None

    def test_get_indexer_returns_indexer_when_available(self, mock_indexer):
        """indexer.available=True 时返回 indexer"""
        assert _get_indexer({"code_indexer": mock_indexer}) is mock_indexer

    def test_get_indexer_returns_none_for_none_value(self):
        """code_indexer=None 时返回 None"""
        assert _get_indexer({"code_indexer": None}) is None

    def test_register_semantic_tools_registers_all(self):
        """register_semantic_tools 注册 4 个工具"""
        registry = MagicMock()
        register_semantic_tools(registry)
        assert registry.register.call_count == 4
        # 验证注册的工具类
        registered_tools = [call.args[0] for call in registry.register.call_args_list]
        tool_names = [t.name for t in registered_tools]
        assert "semantic_search" in tool_names
        assert "get_callers" in tool_names
        assert "get_callees" in tool_names
        assert "find_dead_code" in tool_names

    def test_all_tools_inherit_base_tool(self):
        """所有 4 个工具都继承 BaseTool"""
        tools = [SemanticSearchTool(), GetCallersTool(),
                 GetCalleesTool(), FindDeadCodeTool()]
        for tool in tools:
            assert isinstance(tool, BaseTool)

    def test_all_tools_have_valid_schema(self):
        """所有工具的 schema 都符合 OpenAI function calling 格式"""
        tools = [SemanticSearchTool(), GetCallersTool(),
                 GetCalleesTool(), FindDeadCodeTool()]
        for tool in tools:
            schema = tool.schema
            assert schema["type"] == "function"
            assert "function" in schema
            assert schema["function"]["name"] == tool.name
            assert "description" in schema["function"]
            assert "parameters" in schema["function"]
