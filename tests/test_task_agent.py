"""P1-4 双 Agent 类型测试 — CoderAgent + TaskAgent

测试用例：
1. test_task_agent_readonly_tools: TaskAgent 只暴露只读工具
2. test_task_agent_blocks_write_tools: write_file 被 TaskAgent 阻止
3. test_task_agent_blocks_build_tools: embed_build 被 TaskAgent 阻止
4. test_task_agent_allows_read_tools: read_file 被 TaskAgent 允许
5. test_coder_agent_allows_all_tools: CoderAgent 不限制工具
6. test_system_prompt_prefix: TaskAgent 系统提示包含"只读"
7. test_base_engine_abstract: BaseAgentEngine 不能直接实例化
8. test_agent_inheritance: CoderAgent/AgentEngine 是 BaseAgentEngine 子类
9. test_agent_backward_compat: AgentEngine(...) 仍可用（向后兼容别名）

运行方式: pytest tests/test_task_agent.py -v
"""
import pytest
from pathlib import Path
from types import SimpleNamespace


def _make_config():
    """构建测试用 config（SimpleNamespace，与 test_engine.py 风格一致）"""
    return SimpleNamespace(
        project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
        mcp={},
    )


def _make_engine(engine_class):
    """构建指定类型的 engine 实例（共用工厂，减少重复）"""
    from iron.agent.prompt_builder import PromptBuilder
    from iron.llm.backend import EchoBackend
    from iron.skills.registry import SkillRegistry
    return engine_class(
        llm=EchoBackend(),
        prompt_builder=PromptBuilder(Path(".")),
        skills=SkillRegistry(),
        config=_make_config(),
    )


def _make_schema(name: str) -> dict:
    """构造工具 schema（仅含 name，用于过滤测试）"""
    return {"function": {"name": name}}


# ── 1. TaskAgent 只暴露只读工具 ──────────────────────────────


class TestTaskAgentReadonlyTools:
    """TaskAgent 工具集过滤测试"""

    def test_task_agent_readonly_tools(self):
        """TaskAgent 只暴露只读工具（write_file/embed_build 等不在集合内）"""
        from iron.agent.engine import TaskAgent
        engine = _make_engine(TaskAgent)
        allowed = engine._get_allowed_tools()

        # 只读工具应该在允许集合内
        assert "read_file" in allowed
        assert "search_code" in allowed
        assert "find_files" in allowed
        assert "web_search" in allowed
        assert "embed_lint" in allowed  # P1-4: 静态分析是只读的
        assert "chat" in allowed

        # 写工具不应该在允许集合内
        assert "write_file" not in allowed
        assert "edit_file" not in allowed
        assert "run_command" not in allowed
        assert "embed_build" not in allowed
        assert "embed_flash" not in allowed


# ── 2/3/4. TaskAgent 工具阻止/允许测试 ─────────────────────────


class TestTaskAgentToolFiltering:
    """TaskAgent 工具过滤行为测试（通过 _filter_tools_schema 验证阻止/允许）"""

    def test_task_agent_blocks_write_tools(self):
        """write_file 被 TaskAgent 阻止（schema 过滤后不包含 write_file）"""
        from iron.agent.engine import TaskAgent
        engine = _make_engine(TaskAgent)
        schemas = [_make_schema("write_file"), _make_schema("read_file")]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        assert "write_file" not in names, "write_file 应被 TaskAgent 阻止"
        assert "read_file" in names

    def test_task_agent_blocks_build_tools(self):
        """embed_build/embed_flash 被 TaskAgent 阻止"""
        from iron.agent.engine import TaskAgent
        engine = _make_engine(TaskAgent)
        schemas = [
            _make_schema("embed_build"),
            _make_schema("embed_flash"),
            _make_schema("embed_lint"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        assert "embed_build" not in names, "embed_build 应被 TaskAgent 阻止"
        assert "embed_flash" not in names, "embed_flash 应被 TaskAgent 阻止"
        # embed_lint 是只读静态分析，应该被允许
        assert "embed_lint" in names, "embed_lint（只读分析）应被 TaskAgent 允许"

    def test_task_agent_allows_read_tools(self):
        """read_file/search_code/find_files 被 TaskAgent 允许"""
        from iron.agent.engine import TaskAgent
        engine = _make_engine(TaskAgent)
        schemas = [
            _make_schema("read_file"),
            _make_schema("search_code"),
            _make_schema("find_files"),
            _make_schema("web_search"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        for name in ["read_file", "search_code", "find_files", "web_search"]:
            assert name in names, f"{name} 应被 TaskAgent 允许"


# ── 5. CoderAgent 不限制工具 ──────────────────────────────────


class TestCoderAgentAllTools:
    """CoderAgent（AgentEngine 别名）允许全部工具"""

    def test_coder_agent_allows_all_tools(self):
        """CoderAgent._get_allowed_tools() 返回 None（全部允许）"""
        from iron.agent.engine import CoderAgent
        engine = _make_engine(CoderAgent)
        assert engine._get_allowed_tools() is None

    def test_coder_agent_no_filter(self):
        """CoderAgent 的 _filter_tools_schema 不过滤任何工具"""
        from iron.agent.engine import CoderAgent
        engine = _make_engine(CoderAgent)
        schemas = [
            _make_schema("write_file"),
            _make_schema("embed_build"),
            _make_schema("read_file"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        assert len(filtered) == len(schemas), "CoderAgent 不应过滤任何工具"


# ── 6. TaskAgent 系统提示包含"只读" ────────────────────────────


class TestSystemPromptPrefix:
    """系统提示前缀测试"""

    def test_system_prompt_prefix(self):
        """TaskAgent 系统提示包含"只读"标记"""
        from iron.agent.engine import TaskAgent
        engine = _make_engine(TaskAgent)
        # _get_system_prompt_prefix 应返回包含"只读"的字符串
        prefix = engine._get_system_prompt_prefix()
        assert "只读" in prefix, "TaskAgent 前缀应包含'只读'"

        # 完整系统提示也应包含"只读"
        system_prompt = engine._build_system_prompt()
        assert "只读" in system_prompt, "TaskAgent 系统提示应包含'只读'"

    def test_coder_prompt_no_readonly_marker(self):
        """CoderAgent 系统提示不包含'只读'标记（不改变默认行为）"""
        from iron.agent.engine import CoderAgent
        engine = _make_engine(CoderAgent)
        prefix = engine._get_system_prompt_prefix()
        assert prefix == "", "CoderAgent 前缀应为空字符串"
        assert "只读" not in prefix


# ── 7. BaseAgentEngine 是抽象类 ───────────────────────────────


class TestBaseEngineAbstract:
    """BaseAgentEngine 抽象类测试"""

    def test_base_engine_abstract(self):
        """BaseAgentEngine 不能直接实例化（抽象类）"""
        from iron.agent.engine import BaseAgentEngine
        with pytest.raises(TypeError):
            BaseAgentEngine(
                llm=None,
                prompt_builder=None,
                skills=None,
                config=_make_config(),
            )


# ── 8. 继承关系测试 ───────────────────────────────────────────


class TestAgentInheritance:
    """Agent 类继承关系测试"""

    def test_agent_inheritance(self):
        """CoderAgent/AgentEngine 是 BaseAgentEngine 子类"""
        from iron.agent.engine import (
            BaseAgentEngine, CoderAgent, CoderAgentEngine, AgentEngine,
        )
        assert issubclass(CoderAgent, BaseAgentEngine)
        assert issubclass(CoderAgentEngine, BaseAgentEngine)
        assert issubclass(AgentEngine, BaseAgentEngine)

    def test_task_agent_inheritance(self):
        """TaskAgent 是 BaseAgentEngine 子类"""
        from iron.agent.engine import BaseAgentEngine, TaskAgent, TaskAgentEngine
        assert issubclass(TaskAgent, BaseAgentEngine)
        assert issubclass(TaskAgentEngine, BaseAgentEngine)


# ── 9. 向后兼容测试 ───────────────────────────────────────────


class TestBackwardCompat:
    """向后兼容别名测试"""

    def test_agent_backward_compat(self):
        """现有代码 AgentEngine(...) 仍可用（向后兼容别名）"""
        from iron.agent.engine import AgentEngine, CoderAgent, CoderAgentEngine
        # AgentEngine 应等价于 CoderAgent（别名）
        assert AgentEngine is CoderAgent
        assert AgentEngine is CoderAgentEngine

        # AgentEngine(...) 可以正常实例化（不抛异常）
        engine = _make_engine(AgentEngine)
        assert engine is not None
        assert engine._get_allowed_tools() is None  # 全部允许

    def test_short_aliases(self):
        """CoderAgent/TaskAgent 简短别名等价于 Engine 后缀版本"""
        from iron.agent.engine import (
            CoderAgent, CoderAgentEngine,
            TaskAgent, TaskAgentEngine,
        )
        assert CoderAgent is CoderAgentEngine
        assert TaskAgent is TaskAgentEngine

    def test_task_agent_backward_compat(self):
        """TaskAgentEngine 名称仍可用（向后兼容）"""
        from iron.agent.engine import TaskAgent, TaskAgentEngine
        assert TaskAgent is TaskAgentEngine
        engine = _make_engine(TaskAgentEngine)
        assert engine._get_allowed_tools() is not None  # 只读集合
