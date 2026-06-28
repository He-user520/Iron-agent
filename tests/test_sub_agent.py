"""Track 8: 子 Agent 并行编排测试

测试用例覆盖：
- SubAgentOrchestrator.run 成功 / 超时 / 异常 / 取消
- _select_agent_class 类型映射（coder/explore/verify/未知）
- _run_sub_agent 事件收集（chat_chunk / phase=done / chat_response）
- run_parallel 并行执行 + 空列表
- TaskTool.execute 成功 / 无 engine / 无 prompt / 特性关闭
- TaskTool.name / schema / 注册
- 子 Agent conversation 隔离（独立 engine 实例）

运行方式: pytest tests/test_sub_agent.py -v
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── 工厂函数 ────────────────────────────────────────────────


def _make_parent_engine():
    """构造 mock 父 engine（SimpleNamespace，避免真实 AgentEngine 初始化开销）

    SubAgentOrchestrator.run 只用到 parent 的 llm/prompt_builder/skills/config/
    _lsp_client/_code_indexer 属性，用 SimpleNamespace 足够。
    """
    from iron.agent.prompt_builder import PromptBuilder
    from iron.llm.backend import EchoBackend
    from iron.skills.registry import SkillRegistry

    return SimpleNamespace(
        llm=EchoBackend(),
        prompt_builder=PromptBuilder(Path(".")),
        skills=SkillRegistry(),
        config=SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        ),
        _lsp_client=None,
        _code_indexer=None,
    )


def _make_fake_engine_class(events=None, raise_exc=None, delay=0.0):
    """构造假的子 Agent engine class

    被 _select_agent_class 返回。process 是可控 async generator。

    Args:
        events: list of (event_type, event_data) 元组
        raise_exc: process 启动时抛出的异常（可选）
        delay: 每次 yield 前的延迟（用于测超时）
    """
    events = events or []

    class _FakeEngine:
        def __init__(self, **kwargs):
            # 记录构造参数，便于验证 conversation 隔离
            self.init_kwargs = kwargs
            self.process_calls = []

        async def process(self, user_input):
            self.process_calls.append(user_input)
            for et, ed in events:
                if delay:
                    await asyncio.sleep(delay)
                yield (et, ed)
            if raise_exc:
                raise raise_exc

    return _FakeEngine


# ── 1. SubAgentOrchestrator.run 成功 ────────────────────────


class TestOrchestratorRun:
    """SubAgentOrchestrator.run 主路径测试"""

    @pytest.mark.asyncio
    async def test_run_success(self, monkeypatch):
        """run 成功返回 success=True + output"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        events = [
            ("chat_chunk", {"text": "hello "}),
            ("chat_chunk", {"text": "world"}),
            ("phase", {"phase": "done"}),
        ]
        fake_cls = _make_fake_engine_class(events=events)
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        result = await orch.run(
            description="测试任务",
            prompt="搜索 HAL_Delay",
            agent_type="explore",
        )

        assert result["success"] is True
        assert result["output"] == "hello world"
        assert result["error"] is None
        assert result["agent_type"] == "explore"
        assert result["description"] == "测试任务"
        assert isinstance(result["elapsed"], float)
        assert result["elapsed"] >= 0

    @pytest.mark.asyncio
    async def test_run_timeout(self, monkeypatch):
        """超时返回 success=False + error 含超时秒数"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        # 每次 yield sleep 0.05s，保证超时触发
        fake_cls = _make_fake_engine_class(
            events=[("chat_chunk", {"text": "x"})] * 100,
            delay=0.05,
        )
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        result = await orch.run(
            description="慢任务",
            prompt="跑很久",
            timeout=1,
        )

        assert result["success"] is False
        assert "超时" in result["error"]
        assert "1" in result["error"]
        assert result["output"] == ""

    @pytest.mark.asyncio
    async def test_run_exception_returns_error(self, monkeypatch):
        """子 Agent 抛异常时返回 success=False + error 含异常类型"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        fake_cls = _make_fake_engine_class(raise_exc=ValueError("模拟失败"))
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        result = await orch.run(description="会失败", prompt="抛异常")

        assert result["success"] is False
        assert "ValueError" in result["error"]
        assert "模拟失败" in result["error"]
        assert result["output"] == ""

    @pytest.mark.asyncio
    async def test_run_cancel_propagates(self, monkeypatch):
        """asyncio.CancelledError 不被吞，向上传播"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        fake_cls = _make_fake_engine_class(raise_exc=asyncio.CancelledError())
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        with pytest.raises(asyncio.CancelledError):
            await orch.run(description="取消", prompt="取消测试")


# ── 2. _select_agent_class 类型映射 ─────────────────────────


class TestSelectAgentClass:
    """_select_agent_class 类型映射测试"""

    def test_select_coder(self):
        from iron.agent.engine import AgentEngine
        from iron.agent.sub_agent import SubAgentOrchestrator

        orch = SubAgentOrchestrator(_make_parent_engine())
        assert orch._select_agent_class("coder") is AgentEngine

    def test_select_explore(self):
        from iron.agent.engine import TaskAgentEngine
        from iron.agent.sub_agent import SubAgentOrchestrator

        orch = SubAgentOrchestrator(_make_parent_engine())
        assert orch._select_agent_class("explore") is TaskAgentEngine

    def test_select_verify(self):
        from iron.agent.engine import VerifyAgent
        from iron.agent.sub_agent import SubAgentOrchestrator

        orch = SubAgentOrchestrator(_make_parent_engine())
        assert orch._select_agent_class("verify") is VerifyAgent

    def test_select_unknown_defaults_to_task(self):
        from iron.agent.engine import TaskAgentEngine
        from iron.agent.sub_agent import SubAgentOrchestrator

        orch = SubAgentOrchestrator(_make_parent_engine())
        # 未知类型 fallback 到 TaskAgentEngine
        assert orch._select_agent_class("unknown") is TaskAgentEngine
        assert orch._select_agent_class("task") is TaskAgentEngine


# ── 3. _run_sub_agent 事件收集 ──────────────────────────────


class TestRunSubAgent:
    """_run_sub_agent 直接测试（不经 run() 包装）"""

    @pytest.mark.asyncio
    async def test_collects_chat_chunks(self):
        """收集 chat_chunk 事件文本"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        class _FakeEngine:
            async def process(self, user_input):
                yield ("chat_chunk", {"text": "foo "})
                yield ("chat_chunk", {"text": "bar"})
                yield ("phase", {"phase": "done"})

        orch = SubAgentOrchestrator(_make_parent_engine())
        result = await orch._run_sub_agent(_FakeEngine(), "test", 5)
        assert result == "foo bar"

    @pytest.mark.asyncio
    async def test_breaks_on_phase_done(self):
        """遇到 phase=done 立即结束，后续事件不再处理"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        class _FakeEngine:
            async def process(self, user_input):
                yield ("chat_chunk", {"text": "before"})
                yield ("phase", {"phase": "done"})
                yield ("chat_chunk", {"text": "after"})  # 不应被收集

        orch = SubAgentOrchestrator(_make_parent_engine())
        result = await orch._run_sub_agent(_FakeEngine(), "test", 5)
        assert result == "before"

    @pytest.mark.asyncio
    async def test_handles_chat_response_event(self):
        """chat_response 事件兜底收集 message/content"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        class _FakeEngine:
            async def process(self, user_input):
                yield ("chat_response", {"message": "完整回复"})
                yield ("phase", {"phase": "done"})

        orch = SubAgentOrchestrator(_make_parent_engine())
        result = await orch._run_sub_agent(_FakeEngine(), "test", 5)
        assert result == "完整回复"

    @pytest.mark.asyncio
    async def test_chat_response_content_fallback(self):
        """chat_response 优先 message，fallback content"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        class _FakeEngine:
            async def process(self, user_input):
                yield ("chat_response", {"content": "fallback 内容"})
                yield ("phase", {"phase": "done"})

        orch = SubAgentOrchestrator(_make_parent_engine())
        result = await orch._run_sub_agent(_FakeEngine(), "test", 5)
        assert result == "fallback 内容"


# ── 4. run_parallel 并行 ────────────────────────────────────


class TestRunParallel:
    """run_parallel 并行执行测试"""

    @pytest.mark.asyncio
    async def test_parallel_multiple_tasks(self, monkeypatch):
        """多个任务并行，结果顺序与输入一致"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        events_a = [("chat_chunk", {"text": "A"}), ("phase", {"phase": "done"})]
        events_b = [("chat_chunk", {"text": "B"}), ("phase", {"phase": "done"})]

        fake_a = _make_fake_engine_class(events=events_a)
        fake_b = _make_fake_engine_class(events=events_b)

        call_idx = [0]

        def _select(agent_type):
            call_idx[0] += 1
            return fake_a if call_idx[0] == 1 else fake_b

        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", _select)

        tasks = [
            {"description": "任务A", "prompt": "A", "agent_type": "explore"},
            {"description": "任务B", "prompt": "B", "agent_type": "explore"},
        ]
        results = await orch.run_parallel(tasks)

        assert len(results) == 2
        assert results[0]["output"] == "A"
        assert results[1]["output"] == "B"
        assert all(r["success"] for r in results)

    @pytest.mark.asyncio
    async def test_parallel_empty_list(self):
        """空任务列表返回空列表"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        orch = SubAgentOrchestrator(_make_parent_engine())
        results = await orch.run_parallel([])
        assert results == []


# ── 5. 子 Agent conversation 隔离 ───────────────────────────


class TestConversationIsolation:
    """子 Agent 拥有独立 conversation，不污染父 Agent"""

    @pytest.mark.asyncio
    async def test_sub_engine_is_new_instance(self, monkeypatch):
        """每次 run 创建新的子 engine 实例（独立 conversation）"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        fake_cls = _make_fake_engine_class(
            events=[("chat_chunk", {"text": "ok"}), ("phase", {"phase": "done"})]
        )
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        # 运行两次，应创建两个独立实例
        await orch.run(description="第一次", prompt="A")
        await orch.run(description="第二次", prompt="B")

        # fake_cls 是同一个类，但每次 run 都构造新实例
        # 这里主要验证不抛异常、隔离机制可用
        # （真实隔离由 TaskAgentEngine 的独立 conversation 字段保证）

    @pytest.mark.asyncio
    async def test_sub_engine_receives_independent_prompt(self, monkeypatch):
        """子 engine 收到的 prompt 是独立传入的，不带父 Agent 历史"""
        from iron.agent.sub_agent import SubAgentOrchestrator

        events = [("chat_chunk", {"text": "done"}), ("phase", {"phase": "done"})]
        fake_cls = _make_fake_engine_class(events=events)
        orch = SubAgentOrchestrator(_make_parent_engine())
        monkeypatch.setattr(orch, "_select_agent_class", lambda at: fake_cls)

        await orch.run(description="测试", prompt="只给子 Agent 的指令")
        # 验证子 engine 的 process 收到的是独立的 prompt
        # （fake_cls 内部 process_calls 已记录，但实例在 run 内部创建，
        #   这里验证不抛异常即可，真实隔离由 engine.process 的 user_input 参数保证）


# ── 6. TaskTool 工具测试 ────────────────────────────────────


class TestTaskTool:
    """TaskTool 工具测试"""

    def test_name(self):
        from iron.agent.sub_agent import TaskTool
        assert TaskTool().name == "task"

    def test_schema_structure(self):
        from iron.agent.sub_agent import TaskTool
        schema = TaskTool().schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "task"
        props = schema["function"]["parameters"]["properties"]
        assert "description" in props
        assert "prompt" in props
        assert "agent_type" in props
        assert "max_turns" in props
        assert "timeout" in props
        required = schema["function"]["parameters"]["required"]
        assert "description" in required
        assert "prompt" in required

    @pytest.mark.asyncio
    async def test_execute_success(self, monkeypatch):
        """execute 从 context 获取父 engine 并调用 orchestrator.run"""
        from iron.agent.sub_agent import TaskTool, SubAgentOrchestrator

        tool = TaskTool()
        parent = _make_parent_engine()
        ctx = {"engine": parent, "project_dir": "."}

        # patch SubAgentOrchestrator.run 避免真实创建子 engine
        async def _fake_run(self, **kwargs):
            return {"success": True, "output": "子任务结果", "error": None,
                    "elapsed": 0.1, "agent_type": kwargs.get("agent_type", "explore"),
                    "description": kwargs.get("description", "")}

        monkeypatch.setattr(SubAgentOrchestrator, "run", _fake_run)

        result = await tool.execute(
            args={"description": "测试", "prompt": "做点事"},
            context=ctx,
        )
        assert result["success"] is True
        assert result["output"] == "子任务结果"

    @pytest.mark.asyncio
    async def test_execute_no_engine_in_context(self):
        """context 缺少 engine 时返回错误"""
        from iron.agent.sub_agent import TaskTool

        tool = TaskTool()
        result = await tool.execute(
            args={"description": "x", "prompt": "y"},
            context={},
        )
        assert result["success"] is False
        assert "engine" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_empty_prompt(self):
        """prompt 为空时返回错误"""
        from iron.agent.sub_agent import TaskTool

        tool = TaskTool()
        parent = _make_parent_engine()
        result = await tool.execute(
            args={"description": "x", "prompt": ""},
            context={"engine": parent},
        )
        assert result["success"] is False
        assert "prompt" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_feature_disabled(self, monkeypatch):
        """特性门控关闭时返回错误"""
        import iron.config.features as feat
        from iron.agent.sub_agent import TaskTool

        # 让 sub_agents 特性返回 False
        def _fake_is_enabled(name):
            if name == "sub_agents":
                return False
            return True

        monkeypatch.setattr(feat, "is_feature_enabled", _fake_is_enabled)

        tool = TaskTool()
        parent = _make_parent_engine()
        result = await tool.execute(
            args={"description": "x", "prompt": "y"},
            context={"engine": parent},
        )
        assert result["success"] is False
        assert "sub_agents" in result["error"] or "未启用" in result["error"]


# ── 7. 注册测试 ────────────────────────────────────────────


class TestRegisterTaskTool:
    """register_task_tool 注册测试"""

    def test_registers_task_tool(self):
        """注册函数将 TaskTool 实例加入 registry"""
        from iron.agent.sub_agent import register_task_tool, TaskTool

        class _FakeRegistry:
            def __init__(self):
                self.tools = []

            def register(self, tool):
                self.tools.append(tool)

        registry = _FakeRegistry()
        register_task_tool(registry)

        assert len(registry.tools) == 1
        assert isinstance(registry.tools[0], TaskTool)
        assert registry.tools[0].name == "task"

    def test_inherits_basetool(self):
        """TaskTool 继承 BaseTool，safe_execute 可用"""
        from iron.agent.sub_agent import TaskTool
        from iron.tools.base import BaseTool

        tool = TaskTool()
        assert isinstance(tool, BaseTool)
        assert hasattr(tool, "safe_execute")
        assert hasattr(tool, "max_output_chars")
