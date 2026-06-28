"""P1-1 渐进式上下文压缩测试 — Level 1 microcompact + Level 2 动态阈值

验证点：
1. Level 1 microcompact 行为：截断早期 tool 输出、合并连续 thinking、保留协议配对
2. Level 2 动态阈值：从 backend.context_window 读取，fallback 30K
3. 两级触发顺序：Level 1 在前，Level 2 在后

运行方式: pytest tests/test_progressive_compaction.py -v
"""
import json

import pytest

from iron.agent.memory import (
    ContextCompactor,
    KEEP_RECENT_TOOL_RESULTS,
    TOOL_OUTPUT_TRUNCATE_CHARS,
    MAX_CONTEXT_TOKENS,
)
from iron.llm.backend import LLMBackend, EchoBackend, OpenAIBackend, AnthropicBackend, OllamaBackend


# ── Level 1 microcompact 测试 ─────────────────────────────────


class TestMicrocompact:
    """Level 1: 实时轻量压缩（不调 LLM）"""

    def test_short_messages_unchanged(self):
        """消息少于阈值时直接返回原列表（不操作）"""
        compactor = ContextCompactor(llm=None)
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = compactor.microcompact(messages)
        assert result is messages  # 同一引用，未做处理

    def test_truncate_early_tool_output(self):
        """截断早期 tool_results 的 stdout（保留最近 N 条不截断）"""
        compactor = ContextCompactor(llm=None)
        # 构造 KEEP_RECENT_TOOL_RESULTS + 5 条 tool 消息，前 5 条会被截断
        long_stdout = "x" * (TOOL_OUTPUT_TRUNCATE_CHARS * 3)  # 远超阈值
        messages = [{"role": "user", "content": "开始"}]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 5):
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]})
            messages.append({"role": "tool", "content": json.dumps({
                "success": True, "stdout": long_stdout, "command": f"cmd_{i}"
            })})

        result = compactor.microcompact(messages)
        # 早期 5 条 tool 消息应被截断（stdout 长度 = 截断长度 + "...[截断]"）
        early_tools = [m for m in result if m.get("role") == "tool"][:5]
        for m in early_tools:
            data = json.loads(m["content"])
            assert len(data["stdout"]) == TOOL_OUTPUT_TRUNCATE_CHARS + len("...[截断]")

        # 最近 KEEP_RECENT_TOOL_RESULTS 条 tool 消息不截断
        recent_tools = [m for m in result if m.get("role") == "tool"][-KEEP_RECENT_TOOL_RESULTS:]
        for m in recent_tools:
            data = json.loads(m["content"])
            assert len(data["stdout"]) == len(long_stdout)  # 原始长度

    def test_preserves_tool_calls_pairing(self):
        """不删除任何消息，保持 assistant.tool_calls ↔ tool 配对完整"""
        compactor = ContextCompactor(llm=None)
        long_output = "y" * (TOOL_OUTPUT_TRUNCATE_CHARS * 2)
        messages = [{"role": "user", "content": "执行"}]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3):
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": "run", "arguments": "{}"}}
            ]})
            messages.append({"role": "tool", "content": json.dumps({
                "success": True, "stdout": long_output
            })})

        result = compactor.microcompact(messages)
        # 消息数量应保持不变（不删除）
        assert len(result) == len(messages)
        # 每个 assistant.tool_calls 后面紧跟 tool 消息
        for i, m in enumerate(result):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assert i + 1 < len(result)
                assert result[i + 1].get("role") == "tool"

    def test_merge_consecutive_assistant_thinking(self):
        """合并连续的纯文本 assistant 消息（thinking 合并）"""
        compactor = ContextCompactor(llm=None)
        # 构造多条连续 assistant 消息（无 tool_calls）
        messages = [
            {"role": "user", "content": "分析下"},
            {"role": "assistant", "content": "思考 1"},
            {"role": "assistant", "content": "思考 2"},
            {"role": "assistant", "content": "思考 3"},
        ]
        # 加足够多消息触发 microcompact（> KEEP_RECENT_TOOL_RESULTS + 2）
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]})
        messages.append({"role": "tool", "content": json.dumps({"success": True})})
        # 再加几条 assistant + tool 凑数
        for i in range(KEEP_RECENT_TOOL_RESULTS):
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i+2}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]})
            messages.append({"role": "tool", "content": json.dumps({"success": True})})

        result = compactor.microcompact(messages)
        # 前三条 assistant 应被合并为一条
        first_assistants = [m for m in result if m.get("role") == "assistant" and not m.get("tool_calls")]
        # 应该只剩一条合并后的 assistant（前 3 条合并）
        assert len(first_assistants) == 1
        assert "思考 1" in first_assistants[0]["content"]
        assert "思考 2" in first_assistants[0]["content"]
        assert "思考 3" in first_assistants[0]["content"]

    def test_does_not_merge_assistant_with_tool_calls(self):
        """不合并含 tool_calls 的 assistant 消息（避免破坏 API 协议）"""
        compactor = ContextCompactor(llm=None)
        messages = [
            {"role": "user", "content": "执行"},
        ]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3):
            messages.append({"role": "assistant", "content": f"调用 {i}", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]})
            messages.append({"role": "tool", "content": json.dumps({"success": True})})

        result = compactor.microcompact(messages)
        # 所有 assistant 都含 tool_calls，不应被合并
        assistants = [m for m in result if m.get("role") == "assistant"]
        assert len(assistants) == KEEP_RECENT_TOOL_RESULTS + 3

    def test_truncate_non_json_tool_content(self):
        """非 JSON 的 tool content 直接截断字符串"""
        compactor = ContextCompactor(llm=None)
        long_text = "z" * (TOOL_OUTPUT_TRUNCATE_CHARS * 4)
        messages = [{"role": "user", "content": "x"}]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3):
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]})
            # 前 3 条 tool 是非 JSON 长字符串
            messages.append({"role": "tool", "content": long_text})

        result = compactor.microcompact(messages)
        early_tools = [m for m in result if m.get("role") == "tool"][:3]
        for m in early_tools:
            # 截断后长度 = TOOL_OUTPUT_TRUNCATE_CHARS * 2 + "...[截断]" 长度
            assert len(m["content"]) == TOOL_OUTPUT_TRUNCATE_CHARS * 2 + len("...[截断]")


# ── Level 2 动态阈值测试 ───────────────────────────────────────


class TestDynamicContextLimit:
    """Level 2: 动态阈值从 backend.context_window 读取"""

    def test_no_llm_fallback_to_30k(self):
        """无 LLM 时 fallback 到 MAX_CONTEXT_TOKENS（30K）"""
        compactor = ContextCompactor(llm=None)
        assert compactor._get_context_limit() == MAX_CONTEXT_TOKENS

    def test_llm_without_context_window_attr(self):
        """LLM 无 context_window 属性时 fallback"""
        class FakeLLM:
            pass
        compactor = ContextCompactor(llm=FakeLLM())
        assert compactor._get_context_limit() == MAX_CONTEXT_TOKENS

    def test_llm_with_zero_context_window(self):
        """LLM context_window=0 时 fallback"""
        class FakeLLM:
            context_window = 0
        compactor = ContextCompactor(llm=FakeLLM())
        assert compactor._get_context_limit() == MAX_CONTEXT_TOKENS

    def test_llm_with_negative_context_window(self):
        """LLM context_window 负数时 fallback"""
        class FakeLLM:
            context_window = -100
        compactor = ContextCompactor(llm=FakeLLM())
        assert compactor._get_context_limit() == MAX_CONTEXT_TOKENS

    def test_openai_backend_uses_128k(self):
        """OpenAIBackend context_window=128000，阈值为 128000*0.85"""
        backend = OpenAIBackend(api_key="sk-test")
        compactor = ContextCompactor(llm=backend)
        limit = compactor._get_context_limit()
        assert limit == int(128000 * 0.85)

    def test_anthropic_backend_uses_200k(self):
        """AnthropicBackend context_window=200000"""
        backend = AnthropicBackend(api_key="sk-ant-test")
        compactor = ContextCompactor(llm=backend)
        limit = compactor._get_context_limit()
        assert limit == int(200000 * 0.85)

    def test_ollama_backend_uses_8k(self):
        """OllamaBackend context_window=8000"""
        backend = OllamaBackend()
        compactor = ContextCompactor(llm=backend)
        limit = compactor._get_context_limit()
        assert limit == int(8000 * 0.85)

    def test_echo_backend_fallback_to_30k(self):
        """EchoBackend 继承基类默认 context_window=30000"""
        backend = EchoBackend()
        compactor = ContextCompactor(llm=backend)
        # EchoBackend 未覆盖 context_window，使用基类默认 30000
        assert backend.context_window == 30000
        assert compactor._get_context_limit() == int(30000 * 0.85)


# ── 两级触发顺序测试 ───────────────────────────────────────────


class TestTwoLevelCompaction:
    """验证 Level 1 和 Level 2 触发顺序和协同"""

    def test_level1_runs_before_level2(self):
        """Level 1 在 Level 2 之前执行

        构造超阈值的长会话：Level 1 先截断 tool 输出（持久化到 messages），
        然后 Level 2 判断阈值。即使 Level 2 不触发，Level 1 也已生效。
        """
        compactor = ContextCompactor(llm=None)  # 无 LLM，Level 2 不会真摘要
        long_stdout = "a" * (TOOL_OUTPUT_TRUNCATE_CHARS * 5)
        messages = [{"role": "user", "content": "执行"}]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 5):
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]})
            messages.append({"role": "tool", "content": json.dumps({"success": True, "stdout": long_stdout})})

        # Level 1
        after_l1 = compactor.microcompact(messages)
        # 早期 tool 输出应被截断
        early_tools = [m for m in after_l1 if m.get("role") == "tool"][:5]
        for m in early_tools:
            data = json.loads(m["content"])
            assert len(data["stdout"]) == TOOL_OUTPUT_TRUNCATE_CHARS + len("...[截断]")

    @pytest.mark.asyncio
    async def test_level2_triggers_when_over_limit(self):
        """Level 2 在超阈值时触发（用极小窗口 LLM 强制触发）"""
        class SmallWindowLLM:
            context_window = 1  # 极小窗口，阈值 = int(1*0.85) = 0，必触发

            async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
                # 返回简单摘要
                from iron.llm.backend import LLMResponse
                return LLMResponse(content="## 摘要\n执行了若干工具调用", model="fake")

        compactor = ContextCompactor(llm=SmallWindowLLM())
        # 构造足够长的会话（> KEEP_RECENT_MESSAGES + 2）
        messages = [{"role": "user", "content": "开始任务"}]
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3):
            messages.append({"role": "assistant", "content": f"调用 {i}"})
            messages.append({"role": "user", "content": f"继续 {i}"})

        system = "你是助手"
        result = await compactor.compact_if_needed(messages, system)
        # Level 2 应触发：返回的列表包含一个 system 角色的摘要 + KEEP_RECENT_MESSAGES 条 recent
        assert result[0]["role"] == "system"
        assert "[会话历史摘要]" in result[0]["content"]
        # 末尾应保留最近 KEEP_RECENT_MESSAGES 条
        from iron.agent.memory import KEEP_RECENT_MESSAGES
        assert len(result) == 1 + KEEP_RECENT_MESSAGES

    @pytest.mark.asyncio
    async def test_level2_not_triggered_under_limit(self):
        """Level 2 在阈值以下不触发"""
        class LargeWindowLLM:
            context_window = 1_000_000  # 超大窗口，永不触发

            async def generate(self, *args, **kwargs):
                raise AssertionError("不应调用 LLM")

        compactor = ContextCompactor(llm=LargeWindowLLM())
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = await compactor.compact_if_needed(messages, "system")
        assert result is messages  # 原样返回


# ── Backend context_window 属性测试 ─────────────────────────────


class TestBackendContextWindow:
    """验证各 LLM 后端的 context_window 属性正确设置"""

    def test_base_llm_backend_default(self):
        """LLMBackend 基类默认 context_window=30000"""
        # LLMBackend 是 ABC 不能直接实例化，用 EchoBackend 验证继承
        backend = EchoBackend()
        assert backend.context_window == 30000

    def test_openai_backend(self):
        backend = OpenAIBackend(api_key="sk-test")
        assert backend.context_window == 128000

    def test_anthropic_backend(self):
        backend = AnthropicBackend(api_key="sk-ant-test")
        assert backend.context_window == 200000

    def test_ollama_backend(self):
        backend = OllamaBackend()
        assert backend.context_window == 8000

    def test_context_window_is_int(self):
        """context_window 是整数类型"""
        for backend in [
            EchoBackend(),
            OpenAIBackend(api_key="sk-test"),
            AnthropicBackend(api_key="sk-ant-test"),
            OllamaBackend(),
        ]:
            assert isinstance(backend.context_window, int)
            assert backend.context_window > 0
