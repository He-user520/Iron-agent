"""engine.process() 端到端集成测试

测试完整的 Agent 循环：用户输入 → AI 返回 → 工具执行 → 回复
运行方式: pytest tests/test_engine_integration.py -v
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iron.agent.engine import AgentEngine
from iron.agent.prompt_builder import PromptBuilder
from iron.llm.backend import EchoBackend, LLMBackend, LLMResponse
from iron.skills.registry import SkillRegistry


def _make_engine(tmp_path, llm=None, max_steps=None):
    """构造测试用 AgentEngine，project_dir 指向临时目录"""
    config = SimpleNamespace(
        project=SimpleNamespace(project_dir=str(tmp_path), mcu="stm32f407", build_system="platformio"),
        mcp={},
        # P2-3: 权限黑名单持久化路径指向临时目录，避免污染用户 ~/.iron/permissions.yml
        permission_persist_path=str(tmp_path / "permissions.yml"),
    )
    if max_steps is not None:
        config.max_steps = max_steps
    return AgentEngine(
        llm=llm or EchoBackend(),
        prompt_builder=PromptBuilder(Path(".")),
        skills=SkillRegistry(),
        config=config,
    )


class _ScriptedLLM(LLMBackend):
    """按预设脚本返回响应的 mock LLM，用于精确控制工具调用链"""
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.call_count = 0
        self.received_messages = []

    async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
        self.call_count += 1
        self.received_messages.append(messages)
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        # 超出预设后返回 chat 终止
        return LLMResponse(content="任务完成", model="mock")

    async def stream_generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
        resp = await self.generate(system, messages, temperature, max_tokens, tools)
        if resp.content:
            yield ("chunk", resp.content)
        yield ("response", resp)


def _tool_call(name, **args):
    """构造工具调用 LLMResponse"""
    return LLMResponse(
        content="",
        model="mock",
        tool_calls=[{
            "id": f"call_test",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            }
        }],
    )


async def _consume(engine, user_input):
    """消费 process() 的所有事件，返回事件列表"""
    events = []
    async for event in engine.process(user_input):
        events.append(event)
    return events


class TestProcessEndToEnd:
    """engine.process() 端到端集成测试"""

    @pytest.mark.asyncio
    async def test_chat_response_terminates_loop(self, tmp_path):
        """AI 返回纯文本回复（无工具调用）→ 循环立即结束"""
        llm = _ScriptedLLM([LLMResponse(content="你好，我是助手", model="mock")])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "测试")
        # 应该有 chat_response 事件
        chat_events = [e for e in events if e.type == "chat_response"]
        assert len(chat_events) >= 1
        assert "你好" in chat_events[-1].data["message"]
        # AI 只调用一次（无工具调用，直接终止）
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_tool_call_then_chat(self, tmp_path):
        """AI 返回 read_file 工具调用 → 执行 → AI 再返回 chat 终止"""
        # 先创建一个测试文件
        test_file = tmp_path / "main.c"
        test_file.write_text("int main() { return 0; }", encoding="utf-8")

        llm = _ScriptedLLM([
            _tool_call("read_file", path=str(test_file)),
            LLMResponse(content="文件内容已读取", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "读取 main.c")

        # 第一次调用返回工具调用，第二次返回 chat 终止
        assert llm.call_count == 2
        # 应该有 file_read 事件
        file_reads = [e for e in events if e.type == "file_read"]
        assert len(file_reads) >= 1
        # 应该有 chat_response
        chats = [e for e in events if e.type == "chat_response"]
        assert len(chats) >= 1

    @pytest.mark.asyncio
    async def test_write_file_creates_file(self, tmp_path):
        """AI 调用 write_file → 文件被创建"""
        target = tmp_path / "output.c"
        content = "int main() { return 0; }"
        llm = _ScriptedLLM([
            _tool_call("write_file", path=str(target), content=content),
            LLMResponse(content="文件已创建", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "创建文件")

        # 文件应该被创建
        assert target.exists()
        assert target.read_text(encoding="utf-8") == content
        # 应该有 file_start/file_done 事件
        file_starts = [e for e in events if e.type == "file_start"]
        assert len(file_starts) >= 1

    @pytest.mark.asyncio
    async def test_find_files_returns_results(self, tmp_path):
        """find_files 工具能找到文件"""
        (tmp_path / "a.c").write_text("int a;", encoding="utf-8")
        (tmp_path / "b.c").write_text("int b;", encoding="utf-8")

        llm = _ScriptedLLM([
            _tool_call("find_files", pattern="*.c"),
            LLMResponse(content="找到 2 个文件", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "查找 c 文件")

        assert llm.call_count == 2
        # 不应有 error 事件
        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_search_code_finds_pattern(self, tmp_path):
        """search_code 工具能搜索代码"""
        (tmp_path / "test.c").write_text("int main() { return 0; }", encoding="utf-8")

        llm = _ScriptedLLM([
            _tool_call("search_code", pattern="main"),
            LLMResponse(content="找到匹配", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "搜索 main")

        assert llm.call_count == 2
        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_doom_loop_blocked(self, tmp_path):
        """连续 3 次相同工具调用 → doom_loop 拦截

        注意：read_file 有独立分支不检查 doom_loop，这里用 find_files
        （走 else 分支的外部注册工具，会执行 doom_loop 检测）。
        """
        (tmp_path / "a.c").write_text("x", encoding="utf-8")
        # 连续返回相同 find_files 调用
        llm = _ScriptedLLM([
            _tool_call("find_files", pattern="*.c"),
            _tool_call("find_files", pattern="*.c"),
            _tool_call("find_files", pattern="*.c"),
            LLMResponse(content="完成", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm, max_steps=20)
        events = await _consume(engine, "反复查找")

        # 应该有 step_warn（doom_loop 触发）
        warns = [e for e in events if e.type == "step_warn"]
        assert len(warns) >= 1
        # 第 3 次调用被 doom_loop 拦截，第 4 次返回 chat 终止
        assert llm.call_count <= 4

    @pytest.mark.asyncio
    async def test_max_steps_limit(self, tmp_path):
        """达到 MAX_STEPS 上限时强制终止"""
        # 一直返回工具调用，永不 chat
        target = tmp_path / "a.c"
        target.write_text("x", encoding="utf-8")
        # 使用会变化的参数避免 doom_loop（read_file 无 doom_loop 检查，
        # 但变化参数保持测试意图清晰）
        responses = []
        for i in range(30):
            responses.append(_tool_call("read_file", path=str(target), offset=i))
        llm = _ScriptedLLM(responses)
        engine = _make_engine(tmp_path, llm=llm, max_steps=10)
        events = await _consume(engine, "循环")

        # 达到上限应该有 chat_response（强制终止）
        chats = [e for e in events if e.type == "chat_response"]
        assert len(chats) >= 1
        # 调用次数应受限于 max_steps
        assert llm.call_count <= 12  # 给些余量

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, tmp_path):
        """调用未注册的工具 → 返回错误结果"""
        llm = _ScriptedLLM([
            _tool_call("nonexistent_tool", foo="bar"),
            LLMResponse(content="完成", model="mock"),
        ])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "调用未知工具")

        # 应该有 step_warn（未知工具）
        warns = [e for e in events if e.type == "step_warn"]
        assert len(warns) >= 1
        # 但循环应继续（不崩溃）
        chats = [e for e in events if e.type == "chat_response"]
        assert len(chats) >= 1

    @pytest.mark.asyncio
    async def test_thinking_event_emitted(self, tmp_path):
        """process 应该 yield thinking 事件"""
        llm = _ScriptedLLM([LLMResponse(content="完成", model="mock")])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "测试")

        thinkings = [e for e in events if e.type == "thinking"]
        assert len(thinkings) >= 1

    @pytest.mark.asyncio
    async def test_phase_events_emitted(self, tmp_path):
        """process 应该 yield phase 事件"""
        llm = _ScriptedLLM([LLMResponse(content="完成", model="mock")])
        engine = _make_engine(tmp_path, llm=llm)
        events = await _consume(engine, "测试")

        phases = [e for e in events if e.type == "phase"]
        assert len(phases) >= 1
        # 至少有 think 阶段
        phase_values = [e.data.get("phase") for e in phases]
        assert "think" in phase_values
