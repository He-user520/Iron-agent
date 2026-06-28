"""5 层压缩管道测试 — Level 3/4/5 + compact_pipeline

覆盖任务要求：
- test_level3_context_collapse: 合并连续同类工具结果
- test_level3_keep_recent: 保留最近 N 条不合并
- test_level3_preserve_protocol: 保持 tool_calls ↔ tool 协议配对
- test_level4_auto_compact: 小模型摘要触发（超 0.9 阈值）
- test_level4_fallback: LLM 失败时回退到 Level 2
- test_level5_budget_reduce: 按 token 预算硬裁剪
- test_level5_preserve_recent: 保留 system 摘要 + 最近 N 条
- test_compact_pipeline: 5 层顺序执行

运行方式: pytest tests/test_context_compactor.py -v
"""
import json

import pytest

from iron.agent.memory import (
    ContextCompactor,
    KEEP_RECENT_MESSAGES,
    MAX_CONTEXT_TOKENS,
)
from iron.agent.context_compactor import KEEP_RECENT_TOOL_COLLAPSE
from iron.llm.backend import LLMResponse


# ── Level 3: context_collapse ─────────────────────────────────


class TestLevel3ContextCollapse:
    """Level 3: 合并连续同类工具结果"""

    def test_level3_context_collapse(self):
        """合并连续的同一工具 tool 结果，并标注 [已折叠 N 条同类结果]"""
        compactor = ContextCompactor(llm=None)
        # OpenAI 协议：一个 assistant 携带多个 tool_calls，后跟多条连续 tool 结果
        # 构造：1 user + 1 assistant(N tool_calls) + (N+5) tool
        #   其中前 N=5 条可合并，末尾 5 条受 KEEP_RECENT_TOOL_COLLAPSE 保护
        total_protected = KEEP_RECENT_TOOL_COLLAPSE
        total_collapsible = 5  # 这 5 条应被合并
        n_tool = total_protected + total_collapsible

        # 构造可合并的 5 条 tool（同一 assistant 产出，连续）
        tool_calls_collapsible = [
            {"id": f"call_c{i}", "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}}
            for i in range(total_collapsible)
        ]
        messages = [
            {"role": "user", "content": "执行"},
            {"role": "assistant", "content": "", "tool_calls": tool_calls_collapsible},
        ]
        for i in range(total_collapsible):
            messages.append({
                "role": "tool", "tool_call_id": f"call_c{i}",
                "content": json.dumps({"success": True, "stdout": f"out_{i}"})
            })

        # 构造受保护的 5 条 tool（每个由独立的 assistant 产出，避免被合并）
        for i in range(total_protected):
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"call_p{i}", "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"}
                }]
            })
            messages.append({
                "role": "tool", "tool_call_id": f"call_p{i}",
                "content": json.dumps({"success": True, "stdout": f"prot_{i}"})
            })

        result = compactor.context_collapse(messages)

        # 受保护的最近 N 条 tool 不应被合并
        recent_tools = [m for m in result if m.get("role") == "tool"][-total_protected:]
        for m in recent_tools:
            assert "[已折叠" not in m.get("content", ""), \
                "最近 N 条 tool 不应被合并"

        # 至少有一条 tool 消息被合并（标注 [已折叠 N 条同类结果]）
        collapsed = [m for m in result if m.get("role") == "tool"
                     and "[已折叠" in m.get("content", "")]
        assert len(collapsed) >= 1, "应至少合并一组连续同类 tool 结果"
        # 折叠条数应等于 total_collapsible
        assert f"[已折叠 {total_collapsible} 条同类结果]" in collapsed[0]["content"]

    def test_level3_keep_recent(self):
        """保留最近 KEEP_RECENT_TOOL_COLLAPSE 条 tool 消息不合并"""
        compactor = ContextCompactor(llm=None)
        n_tool = KEEP_RECENT_TOOL_COLLAPSE + 8  # 多出 8 条可合并

        messages = [{"role": "user", "content": "执行"}]
        for i in range(n_tool):
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"c{i}", "type": "function",
                    "function": {"name": "search_code", "arguments": "{}"}
                }]
            })
            messages.append({
                "role": "tool", "tool_call_id": f"c{i}",
                "content": json.dumps({"success": True})
            })

        result = compactor.context_collapse(messages)
        tool_msgs = [m for m in result if m.get("role") == "tool"]

        # 末尾 KEEP_RECENT_TOOL_COLLAPSE 条 tool 必须原样保留
        recent_n = tool_msgs[-KEEP_RECENT_TOOL_COLLAPSE:]
        for m in recent_n:
            assert "[已折叠" not in m["content"], \
                "最近 N 条 tool 不应被合并"
            # 原始 JSON 内容应保留
            data = json.loads(m["content"])
            assert data["success"] is True

    def test_level3_preserve_protocol(self):
        """保持 tool_calls ↔ tool 协议配对完整（无孤儿 tool_call 或孤儿 tool 结果）"""
        compactor = ContextCompactor(llm=None)
        n_tool = KEEP_RECENT_TOOL_COLLAPSE + 4  # 4 条连续同工具可合并

        messages = [{"role": "user", "content": "执行"}]
        for i in range(n_tool):
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"call_{i}", "type": "function",
                    "function": {"name": "f", "arguments": "{}"}
                }]
            })
            messages.append({
                "role": "tool", "tool_call_id": f"call_{i}",
                "content": json.dumps({"success": True})
            })

        result = compactor.context_collapse(messages)

        # 验证协议配对完整：每个 assistant.tool_calls 的每个 id 必须有对应 tool 消息
        all_tool_call_ids = set()
        all_tool_msg_ids = set()
        for m in result:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id"):
                        all_tool_call_ids.add(tc["id"])
            elif m.get("role") == "tool":
                tid = m.get("tool_call_id")
                if tid:
                    all_tool_msg_ids.add(tid)

        # 每个 tool_call_id 必须有对应的 tool 消息（无孤儿 call）
        assert all_tool_call_ids.issubset(all_tool_msg_ids), \
            "存在孤儿 tool_call（无对应 tool 结果）"
        # 每个 tool 消息的 tool_call_id 必须有对应的 assistant.tool_calls（无孤儿 tool）
        assert all_tool_msg_ids.issubset(all_tool_call_ids), \
            "存在孤儿 tool 消息（无对应 tool_call）"


# ── Level 4: auto_compact ────────────────────────────────────


class TestLevel4AutoCompact:
    """Level 4: 调用独立小模型生成结构化摘要"""

    @pytest.mark.asyncio
    async def test_level4_auto_compact(self):
        """超 0.9 阈值时优先用 small_model_llm 生成摘要"""
        # 用极小 context_window 强制超过 0.9 阈值
        class SmallModelLLM:
            context_window = 100  # 极小窗口

            def __init__(self):
                self.generate_called = False

            async def generate(self, system, messages, temperature=0.3,
                               max_tokens=4096, tools=None):
                self.generate_called = True
                return LLMResponse(content="## 摘要\n已用小模型生成", model="small")

        small_model = SmallModelLLM()
        # 主 LLM 不应被调用
        class MainLLM:
            context_window = 100

            async def generate(self, *args, **kwargs):
                raise AssertionError("主 LLM 不应被调用，应优先用 small_model_llm")

        compactor = ContextCompactor(llm=MainLLM(), small_model_llm=small_model)

        # 构造超阈值的长会话（> KEEP_RECENT_MESSAGES + 2 且 token > 100 * 0.85 = 85）
        messages = [{"role": "user", "content": "开始任务"}]
        for i in range(KEEP_RECENT_MESSAGES + 5):
            messages.append({"role": "user", "content": f"用户消息 {i} " * 20})
            messages.append({"role": "assistant", "content": f"助手回复 {i} " * 20})

        result = await compactor.auto_compact(messages, "system prompt")

        # small_model 应被调用
        assert small_model.generate_called, "small_model_llm 应被优先调用"
        # 返回的列表应以 system 摘要开头
        assert result[0]["role"] == "system"
        assert "[会话历史摘要]" in result[0]["content"]
        # last_summary 应被更新
        assert compactor.last_summary == "## 摘要\n已用小模型生成"

    @pytest.mark.asyncio
    async def test_level4_fallback(self):
        """LLM 调用失败时回退到 Level 2 compact_if_needed"""
        class FailingSmallLLM:
            context_window = 100

            async def generate(self, *args, **kwargs):
                raise RuntimeError("小模型故障")

        # 主 LLM 也用 SmallWindowLLM，使 compact_if_needed 也能触发
        class MainLLM:
            context_window = 100

            def __init__(self):
                self.generate_called = False

            async def generate(self, system, messages, temperature=0.3,
                               max_tokens=4096, tools=None):
                self.generate_called = True
                return LLMResponse(content="## Level2 摘要", model="main")

        main_llm = MainLLM()
        compactor = ContextCompactor(llm=main_llm, small_model_llm=FailingSmallLLM())

        # 构造超阈值的长会话
        messages = [{"role": "user", "content": "开始任务"}]
        for i in range(KEEP_RECENT_MESSAGES + 5):
            messages.append({"role": "user", "content": f"用户消息 {i} " * 20})
            messages.append({"role": "assistant", "content": f"助手回复 {i} " * 20})

        result = await compactor.auto_compact(messages, "system prompt")

        # 应该 fallback 到 compact_if_needed，调用主 LLM
        assert main_llm.generate_called, \
            "small_model 失败时应 fallback 到主 LLM (compact_if_needed)"
        # 仍然返回有效的摘要
        assert result[0]["role"] == "system"
        assert "[会话历史摘要]" in result[0]["content"]
        assert compactor.last_summary == "## Level2 摘要"


# ── Level 5: budget_reduce ──────────────────────────────────


class TestLevel5BudgetReduce:
    """Level 5: 按 token 预算硬裁剪"""

    def test_level5_budget_reduce(self):
        """超预算时从最早开始裁剪消息（保留 system 摘要 + 最近 N 条）"""
        compactor = ContextCompactor(llm=None)
        # 构造：1 system + N 早期长消息 + KEEP_RECENT_MESSAGES 条短消息
        # 预算小到能保留 system + recent，但必须删除早期长消息
        recent_msgs = [
            {"role": "user", "content": "recent short"},
            {"role": "assistant", "content": "recent asst"},
            {"role": "user", "content": "recent short2"},
            {"role": "assistant", "content": "recent asst2"},
            {"role": "user", "content": "recent short3"},
            {"role": "assistant", "content": "recent asst3"},
        ]
        assert len(recent_msgs) == KEEP_RECENT_MESSAGES
        # 早期长消息（远超预算，必须被删除）
        early_long_msgs = [
            {"role": "user", "content": "early1 " * 1000},
            {"role": "assistant", "content": "early2 " * 1000},
            {"role": "user", "content": "early3 " * 1000},
            {"role": "assistant", "content": "early4 " * 1000},
        ]
        messages = [
            {"role": "system", "content": "[会话历史摘要]\n摘要内容"},
            *early_long_msgs,
            *recent_msgs,
        ]
        # 预算：足够 system + recent，但不足以容纳 early
        # system (~10 tokens) + 6 recent (~3 tokens each = 18) ≈ 30 tokens
        from iron.agent.memory import estimate_messages_tokens
        recent_total = estimate_messages_tokens(
            [{"role": "system", "content": "[会话历史摘要]\n摘要内容"}] + recent_msgs
        )
        budget = recent_total + 50  # 留 50 tokens 余量
        result = compactor.budget_reduce(messages, budget)
        total = estimate_messages_tokens(result)
        assert total <= budget + 200, \
            f"裁剪后 token 总数应 <= 预算+余量，实际 {total}（预算 {budget}）"
        # system 摘要应被保留
        assert result[0]["role"] == "system"
        assert "[会话历史摘要]" in result[0]["content"]
        # early 长消息应被删除
        for m in result:
            content = m.get("content", "")
            assert "early1 " not in content, "早期长消息应被删除"
            assert "early2 " not in content, "早期长消息应被删除"

    def test_level5_preserve_recent(self):
        """保留最近 KEEP_RECENT_MESSAGES 条消息不被删除"""
        compactor = ContextCompactor(llm=None)
        recent_msgs = [
            {"role": "user", "content": "recent_user_short_1"},
            {"role": "assistant", "content": "recent_asst_short_1"},
            {"role": "user", "content": "recent_user_short_2"},
            {"role": "assistant", "content": "recent_asst_short_2"},
            {"role": "user", "content": "recent_user_short_3"},
            {"role": "assistant", "content": "recent_asst_short_3"},
        ]
        # recent_msgs 应有 KEEP_RECENT_MESSAGES（6）条
        assert len(recent_msgs) == KEEP_RECENT_MESSAGES

        early_msgs = [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "early1 " * 1000},
            {"role": "assistant", "content": "early2 " * 1000},
            {"role": "user", "content": "early3 " * 1000},
            {"role": "assistant", "content": "early4 " * 1000},
        ]
        messages = early_msgs + recent_msgs

        budget = 2000  # 足够保留 recent + 部分 early（recent ~30 tokens，余量充足）
        result = compactor.budget_reduce(messages, budget)

        # 末尾 KEEP_RECENT_MESSAGES 条应原样保留（不被删除）
        # 注意：内容可能被截断（如果单条超预算），但消息本身应存在
        result_recent = result[-KEEP_RECENT_MESSAGES:]
        assert len(result_recent) == KEEP_RECENT_MESSAGES, \
            "末尾 KEEP_RECENT_MESSAGES 条消息应保留"
        # 验证 recent 消息内容未被删除（可能被截断但 role 和存在性保留）
        recent_roles = [m["role"] for m in result_recent]
        expected_roles = [m["role"] for m in recent_msgs]
        assert recent_roles == expected_roles, \
            "末尾 KEEP_RECENT_MESSAGES 条消息顺序和角色应保持"


# ── compact_pipeline ──────────────────────────────────────


class TestCompactPipeline:
    """5 层压缩管道顺序执行"""

    @pytest.mark.asyncio
    async def test_compact_pipeline(self):
        """验证 compact_pipeline 按 Level 1→2→3→4→5 顺序执行"""
        # 用大窗口 LLM 避免触发 Level 2/4 的 LLM 摘要
        # （这样 pipeline 主要是 Level 1 + 3 + 5 在工作）
        class LargeWindowLLM:
            context_window = 1_000_000  # 超大窗口，Level 2/4 不会触发

            async def generate(self, *args, **kwargs):
                raise AssertionError("大窗口 LLM 不应被调用")

        compactor = ContextCompactor(llm=LargeWindowLLM())

        # 构造混合消息：长 tool 输出 + 连续同类 tool + 末尾 recent
        # Level 1 microcompact 仅在 tool 消息数 > KEEP_RECENT_TOOL_RESULTS(10) 时触发
        # 所以需要 > 10 条 tool 消息
        from iron.agent.memory import KEEP_RECENT_TOOL_RESULTS
        long_stdout = "x" * 2000  # 远超 Level 1 截断阈值
        # 构造 KEEP_RECENT_TOOL_RESULTS + 5 条 tool（确保 Level 1 触发）
        # 其中前 5 条可被 Level 3 合并（连续同工具）
        n_collapsible = 5  # 连续同工具可合并
        n_protected_tool = KEEP_RECENT_TOOL_COLLAPSE  # Level 3 保护
        # 为让 Level 1 触发，总 tool 数必须 > KEEP_RECENT_TOOL_RESULTS(10)
        n_total_tool = max(KEEP_RECENT_TOOL_RESULTS + 3, n_collapsible + n_protected_tool)

        # 前 n_collapsible 条：连续同工具（可被 Level 3 合并）
        tool_calls_collapsible = [
            {"id": f"call_c{i}", "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}}
            for i in range(n_collapsible)
        ]
        messages = [
            {"role": "user", "content": "执行"},
            {"role": "assistant", "content": "", "tool_calls": tool_calls_collapsible},
        ]
        for i in range(n_collapsible):
            messages.append({
                "role": "tool", "tool_call_id": f"call_c{i}",
                "content": json.dumps({"success": True, "stdout": long_stdout})
            })

        # 剩余 tool（受 Level 3 保护，由独立 assistant 产出）
        for i in range(n_total_tool - n_collapsible):
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"call_p{i}", "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"}
                }]
            })
            messages.append({
                "role": "tool", "tool_call_id": f"call_p{i}",
                "content": json.dumps({"success": True, "stdout": long_stdout})
            })

        # pipeline 应执行成功
        result = await compactor.compact_pipeline(messages, "system prompt")

        # 验证：
        # 1. 返回值是列表
        assert isinstance(result, list)
        # 2. tool_calls ↔ tool 协议配对完整
        all_call_ids = set()
        all_tool_ids = set()
        for m in result:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id"):
                        all_call_ids.add(tc["id"])
            elif m.get("role") == "tool":
                tid = m.get("tool_call_id")
                if tid:
                    all_tool_ids.add(tid)
        assert all_call_ids == all_tool_ids, \
            "pipeline 后 tool_calls ↔ tool 协议配对应完整"
        # 3. Level 3 应已合并连续同类 tool（应存在 [已折叠 N 条] 标注）
        collapsed = [m for m in result if m.get("role") == "tool"
                     and "[已折叠" in m.get("content", "")]
        assert len(collapsed) >= 1, "Level 3 应合并连续同类 tool 结果"
        # 4. 末尾最近 N 条 tool 应保留原始数据（未被合并）
        recent_tools = [m for m in result if m.get("role") == "tool"][-KEEP_RECENT_TOOL_COLLAPSE:]
        for m in recent_tools:
            assert "[已折叠" not in m.get("content", ""), \
                "末尾 KEEP_RECENT_TOOL_COLLAPSE 条 tool 不应被合并"
