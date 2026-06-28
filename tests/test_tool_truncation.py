"""P4-3: 工具结果截断保护测试

覆盖 BaseTool.safe_execute / _truncate_result 的截断逻辑、异常处理，
以及与 IronConfig / AgentEngine 的集成。

运行方式: pytest tests/test_tool_truncation.py -v
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from iron.tools.base import BaseTool, DEFAULT_MAX_OUTPUT_CHARS


# ── 测试用 Mock 工具 ──────────────────────────────────────────


class _MockTool(BaseTool):
    """可控的 mock 工具 — 可预设 execute 的返回值或异常"""

    def __init__(self, result=None, exc=None, max_output_chars=None, name="mock"):
        super().__init__(max_output_chars=max_output_chars)
        self._name = name
        self._result = result
        self._exc = exc
        self.execute_called = False
        self.execute_args = None
        self.execute_context = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self._name, "parameters": {}}}

    async def execute(self, args: dict, context: dict) -> dict:
        self.execute_called = True
        self.execute_args = args
        self.execute_context = context
        if self._exc is not None:
            raise self._exc
        return self._result


# ── _truncate_result 单元测试 ────────────────────────────────


class TestTruncateStringField:
    """test_truncate_string_field: 字符串字段超阈值被截断"""

    def test_truncate_string_field(self):
        tool = _MockTool(max_output_chars=100)
        long_text = "x" * 250
        result = tool._truncate_result({"success": True, "stdout": long_text})
        assert result["truncated"] is True
        assert "stdout" in result["truncated_fields"]
        # 截断后内容应比原始短
        assert len(result["stdout"]) < len(long_text)
        # 保留前 100 字符
        assert result["stdout"].startswith("x" * 100)
        # 包含截断提示
        assert "截断" in result["stdout"]


class TestTruncateListField:
    """test_truncate_list_field: 列表字段总大小超阈值被截断"""

    def test_truncate_list_field(self):
        tool = _MockTool(max_output_chars=50)
        # 每个元素 20 字符，3 个元素总 60 字符 > 50
        items = [{"text": "a" * 18} for _ in range(3)]
        result = tool._truncate_result({"success": True, "matches": items})
        assert result["truncated"] is True
        assert "matches" in result["truncated_fields"]
        # 保留的元素数 < 原始数
        assert result["matches_truncated"] is True
        assert result["matches_original_count"] == 3
        assert result["matches_kept_count"] < 3
        assert len(result["matches"]) == result["matches_kept_count"]


class TestNoTruncationUnderLimit:
    """test_no_truncation_under_limit: 未超阈值不截断"""

    def test_no_truncation_under_limit(self):
        tool = _MockTool(max_output_chars=1000)
        result = tool._truncate_result({
            "success": True,
            "stdout": "short text",
            "matches": [1, 2, 3],
            "content": "hello",
        })
        # 不应有截断标记
        assert "truncated" not in result
        assert "truncated_fields" not in result
        assert "message" not in result
        # 原始内容不变
        assert result["stdout"] == "short text"
        assert result["matches"] == [1, 2, 3]


class TestTruncationAddsMetadata:
    """test_truncation_adds_metadata: 截断后添加 truncated/truncated_fields 元数据"""

    def test_truncation_adds_metadata(self):
        tool = _MockTool(max_output_chars=50)
        result = tool._truncate_result({
            "success": True,
            "content": "x" * 100,
        })
        assert result["truncated"] is True
        assert isinstance(result["truncated_fields"], list)
        assert "content" in result["truncated_fields"]


class TestTruncationMessage:
    """test_truncation_message: 截断消息提示包含阈值和引导"""

    def test_truncation_message(self):
        tool = _MockTool(max_output_chars=200)
        result = tool._truncate_result({"success": True, "output": "y" * 500})
        assert "message" in result
        msg = result["message"]
        # 消息包含阈值数字
        assert "200" in msg
        # 消息引导 AI 用更具体的查询
        assert "更具体" in msg or "截断" in msg


class TestTruncateMultipleFields:
    """test_truncate_multiple_fields: 多字段同时截断"""

    def test_truncate_multiple_fields(self):
        tool = _MockTool(max_output_chars=50)
        result = tool._truncate_result({
            "success": True,
            "stdout": "a" * 100,
            "stderr": "b" * 100,
            "content": "c" * 100,
        })
        assert result["truncated"] is True
        # 三个字段都应被记录
        assert set(result["truncated_fields"]) == {"stdout", "stderr", "content"}
        # 每个字段都被截断
        assert len(result["stdout"]) < 100
        assert len(result["stderr"]) < 100
        assert len(result["content"]) < 100


# ── safe_execute 单元测试 ─────────────────────────────────────


class TestSafeExecuteSuccess:
    """test_safe_execute_success: 正常执行返回结果（含截断检查）"""

    def test_safe_execute_success(self):
        tool = _MockTool(result={"success": True, "data": "ok"})
        result = asyncio.run(tool.safe_execute({}, {}))
        assert result["success"] is True
        assert result["data"] == "ok"
        assert tool.execute_called is True


class TestSafeExecuteException:
    """test_safe_execute_exception: execute 抛异常时 safe_execute 捕获并返回错误"""

    def test_safe_execute_exception(self):
        tool = _MockTool(exc=ValueError("boom"))
        result = asyncio.run(tool.safe_execute({}, {}))
        assert result["success"] is False
        assert "ValueError" in result["error"]
        assert "boom" in result["error"]
        assert result["truncated"] is False


class TestSafeExecuteCancelled:
    """test_safe_execute_cancelled: CancelledError 正常传播不被捕获"""

    def test_safe_execute_cancelled(self):
        tool = _MockTool(exc=asyncio.CancelledError("cancelled"))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(tool.safe_execute({}, {}))


class TestCustomThreshold:
    """test_custom_threshold: 自定义 max_output_chars 阈值生效"""

    def test_custom_threshold(self):
        # 阈值 200 → 150 字符不截断
        tool = _MockTool(max_output_chars=200)
        result = tool._truncate_result({"success": True, "content": "z" * 150})
        assert "truncated" not in result

        # 阈值 50 → 150 字符截断
        tool2 = _MockTool(max_output_chars=50)
        result2 = tool2._truncate_result({"success": True, "content": "z" * 150})
        assert result2["truncated"] is True


# ── 配置项测试 ────────────────────────────────────────────────


class TestConfigSetting:
    """test_config_setting: IronConfig 包含 tool_output_max_chars 配置项"""

    def test_config_default_value(self):
        from iron.config.settings import IronConfig
        config = IronConfig()
        assert hasattr(config, "tool_output_max_chars")
        assert config.tool_output_max_chars == 10000

    def test_config_setting(self):
        from iron.config.settings import IronConfig
        config = IronConfig()
        # 默认值
        assert config.tool_output_max_chars == 10000
        # 可修改
        config.tool_output_max_chars = 5000
        assert config.tool_output_max_chars == 5000


# ── engine 集成测试 ───────────────────────────────────────────


class TestEngineIntegration:
    """test_integration_with_engine: engine 读取配置并应用到注册工具"""

    def _make_engine(self, tool_max_chars=None):
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        if tool_max_chars is not None:
            config.tool_output_max_chars = tool_max_chars
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_engine_reads_config_threshold(self):
        """engine 从 config.tool_output_max_chars 读取阈值"""
        engine = self._make_engine(tool_max_chars=5000)
        assert engine._tool_max_chars == 5000

    def test_engine_default_threshold(self):
        """无配置时 engine 使用默认阈值 10000"""
        engine = self._make_engine()
        assert engine._tool_max_chars == 10000

    def test_engine_applies_threshold_to_tools(self):
        """engine 把阈值应用到所有注册工具"""
        engine = self._make_engine(tool_max_chars=3000)
        # 检查注册的每个工具都有正确的 max_output_chars
        for tool_name in engine._tool_registry.tool_names():
            tool = engine._tool_registry.get(tool_name)
            assert tool.max_output_chars == 3000, f"工具 {tool_name} 阈值未设置"

    def test_integration_with_engine(self):
        """engine 的工具通过 safe_execute 执行（验证 max_output_chars 已传播）"""
        engine = self._make_engine(tool_max_chars=500)
        # 取一个已注册工具验证阈值已设置
        tool = engine._tool_registry.get("search_code")
        assert tool is not None
        assert tool.max_output_chars == 500
        # safe_execute 方法存在且可调用
        assert hasattr(tool, "safe_execute")
        assert callable(tool.safe_execute)
