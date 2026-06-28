"""ToolSearchTool 单元测试 — P4-1 工具动态发现

覆盖：
- 关键词匹配（read/build/flash/search）
- 描述匹配补充
- 无匹配 / 空查询 / limit 限制
- should_use_search_mode 阈值判断
- build_search_mode_tools 搜索模式工具列表
- 完整 execute 流程
- 与 ToolRegistry 集成

运行方式: pytest tests/test_tool_search.py -v
"""
import asyncio

from iron.agent.engine_builtins import BUILTIN_SCHEMAS
from iron.tools import create_default_registry
from iron.tools.tool_search import (
    ToolSearchTool,
    ToolMatch,
    should_use_search_mode,
    build_search_mode_tools,
    SEARCH_MODE_THRESHOLD,
)


def _build_full_schemas() -> list[dict]:
    """构建完整的工具 schema 列表（builtin + 注册工具）"""
    registry = create_default_registry()
    return BUILTIN_SCHEMAS + registry.get_all_schemas()


class TestKeywordMatch:
    """关键词匹配 — 嵌入式关键词映射"""

    def _make_tool(self) -> ToolSearchTool:
        return ToolSearchTool(_build_full_schemas())

    def test_keyword_match_read(self):
        """'读取文件' 匹配 read_file"""
        tool = self._make_tool()
        matches = tool._keyword_match("读取文件")
        names = [m.name for m in matches]
        assert "read_file" in names
        # 关键词匹配得满分
        for m in matches:
            assert m.score == 1.0

    def test_keyword_match_build(self):
        """'编译项目' 匹配 embed_build"""
        tool = self._make_tool()
        matches = tool._keyword_match("编译项目")
        names = [m.name for m in matches]
        assert "embed_build" in names

    def test_keyword_match_flash(self):
        """'烧录' 匹配 embed_flash"""
        tool = self._make_tool()
        matches = tool._keyword_match("烧录")
        names = [m.name for m in matches]
        assert "embed_flash" in names

    def test_keyword_match_search(self):
        """'搜索代码' 匹配 search_code"""
        tool = self._make_tool()
        matches = tool._keyword_match("搜索代码")
        names = [m.name for m in matches]
        assert "search_code" in names


class TestDescriptionMatch:
    """描述匹配 — 补充关键词没匹配到的工具"""

    def test_description_match(self):
        """描述匹配补充：查询词出现在工具描述中但未命中关键词"""
        tool = ToolSearchTool(_build_full_schemas())
        # 'file' 在多个工具描述中出现，但不一定命中中文关键词
        matches = tool._description_match("file", [])
        # 应有至少一个匹配（find_files/read_file 等描述含 file）
        assert len(matches) > 0
        # 描述匹配得分不超过 0.5（降权）
        for m in matches:
            assert 0 < m.score <= 0.5


class TestEdgeCases:
    """边界情况"""

    def test_no_match(self):
        """无匹配返回空列表"""
        tool = ToolSearchTool(_build_full_schemas())
        matches = tool._keyword_match("zzzqqq无意义查询xyz")
        # 关键词不匹配
        assert matches == []

    def test_limit(self):
        """限制返回数量"""
        tool = ToolSearchTool(_build_full_schemas())
        result = asyncio.run(tool.execute(
            {"query": "文件 读取 搜索 编译", "limit": 2}, {}
        ))
        assert result["success"] is True
        assert len(result["tools"]) <= 2

    def test_empty_query(self):
        """空查询返回空结果"""
        tool = ToolSearchTool(_build_full_schemas())
        result = asyncio.run(tool.execute({"query": ""}, {}))
        assert result["success"] is True
        assert result["tools"] == []
        assert "message" in result


class TestShouldUseSearchMode:
    """should_use_search_mode 阈值判断"""

    def test_should_use_search_mode(self):
        """超过阈值返回 True，未超过返回 False"""
        # 未超过 → False
        assert should_use_search_mode(5000, 3000, threshold=20000) is False
        # 刚好等于阈值 → False（严格大于才触发）
        assert should_use_search_mode(15000, 5000, threshold=20000) is False
        # 超过阈值 → True
        assert should_use_search_mode(15000, 6000, threshold=20000) is True
        # 默认阈值 SEARCH_MODE_THRESHOLD
        assert should_use_search_mode(SEARCH_MODE_THRESHOLD + 1, 0) is True
        assert should_use_search_mode(SEARCH_MODE_THRESHOLD - 1, 0) is False


class TestBuildSearchModeTools:
    """build_search_mode_tools 搜索模式工具列表"""

    def test_build_search_mode_tools(self):
        """构建搜索模式工具列表 — 返回 ToolSearchTool 实例"""
        tools = build_search_mode_tools(_build_full_schemas())
        assert len(tools) >= 1
        # 第一个工具是 ToolSearchTool
        search_tool = tools[0]
        assert isinstance(search_tool, ToolSearchTool)
        assert search_tool.name == "tool_search"
        # schema 包含 query 和 limit 参数
        params = search_tool.schema["function"]["parameters"]["properties"]
        assert "query" in params
        assert "limit" in params


class TestExecute:
    """完整 execute 流程"""

    def test_tool_search_execute(self):
        """完整执行流程：query → 匹配 → 排序 → 返回带 schema 的结果"""
        tool = ToolSearchTool(_build_full_schemas())
        result = asyncio.run(tool.execute(
            {"query": "读取文件", "limit": 5}, {}
        ))
        assert result["success"] is True
        assert result["query"] == "读取文件"
        assert result["total_matches"] > 0
        assert len(result["tools"]) > 0
        # 每个工具结果包含必要字段
        first = result["tools"][0]
        assert "name" in first
        assert "description" in first
        assert "schema" in first
        assert "score" in first
        # schema 是 OpenAI function calling 格式
        assert first["schema"]["type"] == "function"
        assert "function" in first["schema"]


class TestWithRegistry:
    """与 ToolRegistry 集成"""

    def test_tool_search_with_registry(self):
        """ToolSearchTool 接受 ToolRegistry 实例并正确搜索"""
        registry = create_default_registry()
        tool = ToolSearchTool(registry)
        # 搜索注册工具（embed_build 在 registry 中）
        result = asyncio.run(tool.execute(
            {"query": "编译", "limit": 5}, {}
        ))
        assert result["success"] is True
        names = [t["name"] for t in result["tools"]]
        assert "embed_build" in names
        # registry 中的工具 schema 是完整格式
        for t in result["tools"]:
            assert t["schema"]["type"] == "function"
