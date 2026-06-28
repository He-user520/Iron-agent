"""上下文压缩器 — 5 层渐进式压缩管道（参考 Claude Code 5 级漏斗）

管道执行顺序（每层超阈值才触发，避免无谓计算）：
    Level 1: microcompact   — 实时轻量压缩（不调 LLM）：截断早期 tool 输出、合并连续 thinking
    Level 2: compact_if_needed — 超阈值时调 LLM 生成摘要（兜底）
    Level 3: context_collapse   — 合并连续同类工具结果，减少消息数量
    Level 4: auto_compact       — 调用独立小模型生成结构化摘要（超 0.9 阈值）
    Level 5: budget_reduce      — 按 token 预算硬裁剪（最后防线）

安全性：
- 所有层保持 OpenAI tool_calls ↔ tool 协议配对完整
- async 方法均处理 asyncio.CancelledError（向上传播）
- LLM 不可用（self.llm is None）时各层优雅降级
"""
import asyncio
import json
import logging

import httpx

# 从 memory.py 导入共享的 token 估算函数和常量（避免重复定义）
from iron.agent.memory import (
    estimate_tokens,
    estimate_messages_tokens,
    serialize_message,
    MAX_CONTEXT_TOKENS,
    KEEP_RECENT_MESSAGES,
    SUMMARY_MAX_TOKENS,
    KEEP_RECENT_TOOL_RESULTS,
    TOOL_OUTPUT_TRUNCATE_CHARS,
    SUMMARY_TEMPLATE,
)

# Level 3 专用常量：保留最近 N 条 tool 消息不合并
KEEP_RECENT_TOOL_COLLAPSE = 5


class ContextCompactor:
    """上下文压缩器 — 5 层渐进式压缩管道

    Level 1: microcompact — 每次请求前实时执行，不调 LLM
        - 截断早期 tool_results 的 stdout/stderr 到 500 字符
        - 合并连续的纯文本 assistant 消息（thinking 合并）
        - 保留最近 N 条 tool_results 不截断
        - 不删除任何消息（保持 OpenAI tool_calls ↔ tool 协议配对）

    Level 2: compact_if_needed — 超阈值时兜底触发，调 LLM 生成摘要
        - 阈值动态读取：优先用 backend.context_window * 0.85
        - 否则 fallback MAX_CONTEXT_TOKENS（30K）
        - 保留最近 KEEP_RECENT_MESSAGES 条不压缩

    Level 3: context_collapse — 合并连续的同类工具结果
        - 检测连续的 tool 角色消息（来自同一工具名），合并为一个摘要消息
        - 保留最近 KEEP_RECENT_TOOL_COLLAPSE 条 tool 消息不合并
        - 合并后标注 [已折叠 N 条同类结果]
        - 保持 tool_calls ↔ tool 协议配对完整

    Level 4: auto_compact — 调用独立小模型生成结构化摘要
        - 优先用 small_model_llm（如果设置），否则用主模型
        - 当 token 超过阈值 0.9 时触发
        - 失败时 fallback 到 Level 2 的 compact_if_needed

    Level 5: budget_reduce — 按 token 预算硬裁剪
        - 从最早的消息开始裁剪（但保留 system 摘要 + 最近 N 条）
        - 每条消息超预算则截断 content，仍超则删除（保持 tool_calls 配对）
    """

    def __init__(self, llm=None, small_model_llm=None):
        """初始化压缩器

        Args:
            llm: 主 LLM 后端（Level 2/4 使用）
            small_model_llm: 轻量小模型后端（Level 4 优先使用，如配置了 config.llm.small_model）
        """
        self.llm = llm
        self._small_model_llm = small_model_llm
        self._last_summary: str = ""  # 上一次的摘要

    @property
    def last_summary(self) -> str:
        """暴露给外部（如 engine.py）的公共只读属性。"""
        return self._last_summary

    def _get_context_limit(self) -> int:
        """动态获取上下文 token 预算

        优先从 backend 读取 context_window（OpenAI=128K, Anthropic=200K, Ollama=8K），
        乘以 0.85 留 15% 余量给 system prompt + 输出。
        backend 不可用或无 context_window 属性时 fallback 到 MAX_CONTEXT_TOKENS（30K）。
        """
        if self.llm is not None:
            ctx_window = getattr(self.llm, "context_window", None)
            if isinstance(ctx_window, (int, float)) and ctx_window > 0:
                # 留 15% 余量给 system prompt 和输出
                return int(ctx_window * 0.85)
        return MAX_CONTEXT_TOKENS

    # ── Level 1: microcompact ────────────────────────────────────

    def microcompact(self, messages: list[dict]) -> list[dict]:
        """Level 1: 实时轻量压缩（不调 LLM，每次请求前调用）

        操作：
        1. 截断早期 tool_results 的 stdout/stderr/content 到 TOOL_OUTPUT_TRUNCATE_CHARS
           （保留最近 KEEP_RECENT_TOOL_RESULTS 条不截断）
        2. 合并连续的纯文本 assistant 消息（thinking 合并）

        安全性：
        - 不删除任何消息，保持 OpenAI tool_calls ↔ tool 协议配对完整
        - 不合并含 tool_calls 的 assistant 消息（会破坏 API 协议）
        - 消息少于阈值时直接返回（不操作）

        Args:
            messages: 当前对话消息列表

        Returns:
            压缩后的消息列表（可能是同一引用，也可能新建）
        """
        if len(messages) <= KEEP_RECENT_TOOL_RESULTS + 2:
            return messages

        # 找到所有 tool 消息的索引
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        # 计算从哪个索引开始截断（保留最近 N 条 tool 消息不截断）
        truncate_before_idx = -1
        if len(tool_indices) > KEEP_RECENT_TOOL_RESULTS:
            # 保留最后 KEEP_RECENT_TOOL_RESULTS 条不截断
            keep_from = tool_indices[-KEEP_RECENT_TOOL_RESULTS]
            # 早期 tool 消息（索引 < keep_from）需要截断输出
            truncate_before_idx = keep_from

        # 1. 截断早期 tool_results 的输出
        result = []
        for i, msg in enumerate(messages):
            if (msg.get("role") == "tool"
                    and truncate_before_idx > 0
                    and i < truncate_before_idx):
                result.append(self._truncate_tool_output(msg))
            else:
                result.append(msg)

        # 2. 合并连续的纯文本 assistant 消息（thinking 合并）
        merged = []
        for msg in result:
            if (msg.get("role") == "assistant"
                    and not msg.get("tool_calls")
                    and merged
                    and merged[-1].get("role") == "assistant"
                    and not merged[-1].get("tool_calls")):
                prev = merged[-1]
                prev_content = prev.get("content", "") or ""
                cur_content = msg.get("content", "") or ""
                # 合并 content，保留 prev 的其他字段
                merged[-1] = {**prev, "content": prev_content + "\n" + cur_content}
            else:
                merged.append(msg)

        return merged

    @staticmethod
    def _truncate_tool_output(msg: dict) -> dict:
        """截断 tool_result 消息中的 stdout/stderr/content 字段

        Args:
            msg: tool 角色消息，content 通常是 JSON 字符串

        Returns:
            截断后的消息（如无修改则原样返回）
        """
        content = msg.get("content")
        if not isinstance(content, str):
            return msg

        # 尝试解析为 JSON（iron 工具结果通常是 JSON 格式）
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON content，直接截断字符串
            if len(content) > TOOL_OUTPUT_TRUNCATE_CHARS * 2:
                return {**msg, "content": content[:TOOL_OUTPUT_TRUNCATE_CHARS * 2] + "...[截断]"}
            return msg

        if not isinstance(data, dict):
            return msg

        modified = False
        for key in ("stdout", "stderr", "output", "content", "result"):
            val = data.get(key)
            if isinstance(val, str) and len(val) > TOOL_OUTPUT_TRUNCATE_CHARS:
                data[key] = val[:TOOL_OUTPUT_TRUNCATE_CHARS] + "...[截断]"
                modified = True

        if modified:
            return {**msg, "content": json.dumps(data, ensure_ascii=False)}
        return msg

    # ── Level 2: compact_if_needed ──────────────────────────────

    async def compact_if_needed(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """Level 2: 兜底压缩 — 超阈值时调 LLM 生成摘要

        阈值动态从 backend.context_window 读取（× 0.85），否则 fallback 30K。

        Returns:
            压缩后的消息列表（可能不变）
        """
        total_tokens = estimate_messages_tokens(messages)
        system_tokens = estimate_tokens(system_prompt)

        # 动态阈值：优先用 backend 窗口大小
        context_limit = self._get_context_limit()

        # 没超限，不压缩
        if total_tokens + system_tokens < context_limit:
            return messages

        # 太短不值得压缩
        if len(messages) <= KEEP_RECENT_MESSAGES + 2:
            return messages

        # 分离：旧消息（需要压缩） + 新消息（保留）
        recent = messages[-KEEP_RECENT_MESSAGES:]
        old = messages[:-KEEP_RECENT_MESSAGES]

        # 序列化旧消息
        serialized = [s for s in (serialize_message(m) for m in old) if s]
        old_text = "\n\n".join(serialized)

        if not old_text.strip():
            return messages

        # 用 LLM 生成摘要（如果可用）
        summary = ""
        if self.llm:
            try:
                if self._last_summary:
                    summary_prompt = f"""更新以下摘要，合并新的对话历史。保留仍然正确的细节，删除过时的信息。

上一次摘要：
{self._last_summary}

新的对话历史：
{old_text}

{SUMMARY_TEMPLATE}"""
                else:
                    summary_prompt = f"{SUMMARY_TEMPLATE}\n\n对话历史：\n{old_text}"

                resp = await self.llm.generate(
                    "你是上下文压缩器。只输出结构化摘要，不要其他文字。",
                    [{"role": "user", "content": summary_prompt}],
                    temperature=0.1,
                    max_tokens=SUMMARY_MAX_TOKENS,
                )
                summary = resp.content.strip()
            except asyncio.CancelledError:
                raise
            except (RuntimeError, httpx.HTTPError) as e:
                logging.warning(f"LLM 摘要失败: {e}", exc_info=True)

        # 如果 LLM 不可用或失败，用简单截断
        if not summary:
            summary = self._simple_summary(old)

        self._last_summary = summary

        # 构造压缩后的消息列表
        compacted = [
            {
                "role": "system",
                "content": f"[会话历史摘要]\n{summary}",
            },
            *recent,
        ]
        return compacted

    def _simple_summary(self, messages: list[dict]) -> str:
        """简单摘要（LLM 不可用时的降级方案）"""
        user_inputs = []
        tool_results = []
        for msg in messages:
            if msg.get("role") == "user":
                user_inputs.append(msg["content"][:100])
            elif msg.get("role") == "tool":
                try:
                    data = json.loads(msg["content"])
                    tool_results.append({
                        "success": data.get("success", True),
                        "command": data.get("command", data.get("path", "")),
                    })
                except (json.JSONDecodeError, TypeError):
                    pass

        parts = ["## 已执行的操作"]
        for i, inp in enumerate(user_inputs[-5:]):
            parts.append(f"- 用户: {inp}")
        for tr in tool_results[-5:]:
            status = "✓" if tr["success"] else "✗"
            parts.append(f"- {status} {tr['command']}")

        return "\n".join(parts)

    # ── Level 3: context_collapse ────────────────────────────────

    def context_collapse(self, messages: list[dict]) -> list[dict]:
        """Level 3: 合并连续的同类工具结果，减少消息数量

        操作：
        - 检测连续的 tool 角色消息（来自同一工具名），合并为一个摘要消息
        - 保留最近 KEEP_RECENT_TOOL_COLLAPSE 条 tool 消息不合并
        - 合并后标注 [已折叠 N 条同类结果]
        - 不删除任何 user/assistant 消息

        安全性：
        - 保持 tool_calls ↔ tool 协议配对完整
        - 合并 N 个 tool 结果时，同时合并 assistant 中对应的 N 个 tool_calls 为 1 个
        - 避免孤儿 tool_call 或孤儿 tool 结果

        Args:
            messages: 当前对话消息列表

        Returns:
            合并后的消息列表（可能不变）
        """
        if len(messages) <= KEEP_RECENT_MESSAGES + 2:
            return messages

        # 找到所有 tool 消息的索引
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        # tool 消息太少，不值得合并
        if len(tool_indices) <= KEEP_RECENT_TOOL_COLLAPSE:
            return messages

        # 保留最近 KEEP_RECENT_TOOL_COLLAPSE 条 tool 消息不合并
        # collapse_before = 最近第 N 条 tool 消息在 messages 中的索引
        collapse_before = tool_indices[-KEEP_RECENT_TOOL_COLLAPSE]

        # 构建 tool_call_id → tool_name 映射（从所有 assistant.tool_calls 中收集）
        call_id_to_name: dict[str, str] = {}
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    tc_name = tc.get("function", {}).get("name", "")
                    if tc_id:
                        call_id_to_name[tc_id] = tc_name

        result: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]

            # 超过 collapse_before 索引的消息不合并（保留最近 N 条 tool）
            if i >= collapse_before:
                result.append(msg)
                i += 1
                continue

            # 非 tool 消息，原样保留
            if msg.get("role") != "tool":
                result.append(msg)
                i += 1
                continue

            # 收集连续的 tool 消息组（直到遇到非 tool 消息或到达 collapse_before）
            group = [msg]
            j = i + 1
            while j < collapse_before and j < len(messages) and messages[j].get("role") == "tool":
                group.append(messages[j])
                j += 1

            # 组内只有 1 条，无需合并
            if len(group) <= 1:
                result.extend(group)
                i = j
                continue

            # 检查组内所有 tool 消息是否来自同一工具名
            tool_names: set[str] = set()
            for g in group:
                tid = g.get("tool_call_id", "")
                name = call_id_to_name.get(tid, "")
                if name:
                    tool_names.add(name)

            if len(tool_names) == 1:
                # 同一工具，可以合并
                tool_name = tool_names.pop()
                merged_content = self._merge_tool_contents(group, tool_name)
                first_call_id = group[0].get("tool_call_id")

                # 合并对应 assistant 的 tool_calls
                # result[-1] 应为产出这些 tool 消息的 assistant
                if (result and result[-1].get("role") == "assistant"
                        and result[-1].get("tool_calls")):
                    prev_asst = result[-1]
                    tool_calls = prev_asst["tool_calls"]
                    group_call_ids = {g.get("tool_call_id") for g in group}
                    matched_calls = [tc for tc in tool_calls if tc.get("id") in group_call_ids]
                    remaining_calls = [tc for tc in tool_calls if tc.get("id") not in group_call_ids]

                    if matched_calls:
                        # 合并为第一个 tool_call（保留其 ID 和 function 信息）
                        merged_call = matched_calls[0]
                        new_tool_calls = [merged_call] + remaining_calls
                        result[-1] = {**prev_asst, "tool_calls": new_tool_calls}

                # 添加合并后的 tool 消息
                collapsed_msg = {
                    "role": "tool",
                    "tool_call_id": first_call_id,
                    "content": f"[已折叠 {len(group)} 条同类结果]\n{merged_content}",
                }
                result.append(collapsed_msg)
            else:
                # 不同工具，不合并，保留原样
                result.extend(group)

            i = j

        return result

    @staticmethod
    def _merge_tool_contents(group: list[dict], tool_name: str) -> str:
        """合并多个 tool 消息的内容为摘要文本

        Args:
            group: 同一工具的连续 tool 消息列表
            tool_name: 工具名

        Returns:
            合并后的摘要文本
        """
        parts = [f"工具: {tool_name}"]
        for idx, msg in enumerate(group):
            content = msg.get("content", "")
            try:
                data = json.loads(content) if isinstance(content, str) else content
                if isinstance(data, dict):
                    success = data.get("success", True)
                    cmd = data.get("command", data.get("path", ""))
                    status = "✓" if success else "✗"
                    info = f"[{idx + 1}] {status}"
                    if cmd:
                        info += f" {cmd}"
                    # 截断输出到 200 字符
                    stdout = (data.get("stdout") or data.get("content") or "")[:200]
                    if stdout:
                        info += f": {stdout}"
                    parts.append(info)
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            # 非 JSON 或解析失败，直接截断
            parts.append(f"[{idx + 1}] {str(content)[:200]}")
        return "\n".join(parts)

    # ── Level 4: auto_compact ────────────────────────────────────

    async def auto_compact(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """Level 4: 调用独立小模型生成结构化摘要

        - 优先用 small_model_llm（如果设置），否则用主模型
        - 当 token 超过阈值 0.9 时触发
        - 保留最近 KEEP_RECENT_MESSAGES 条
        - 旧消息用 LLM 生成结构化摘要（决策、工具结果、关键信息）
        - 失败时 fallback 到 Level 2 的 compact_if_needed

        Args:
            messages: 当前对话消息列表
            system_prompt: 系统提示词

        Returns:
            压缩后的消息列表（可能不变）
        """
        # 选择 LLM：优先 small_model_llm，否则主模型
        llm = self._small_model_llm or self.llm
        if llm is None:
            return messages

        context_limit = self._get_context_limit()
        total_tokens = estimate_messages_tokens(messages) + estimate_tokens(system_prompt)

        # 超 0.9 阈值才触发
        if total_tokens < context_limit * 0.9:
            return messages

        # 太短不值得压缩
        if len(messages) <= KEEP_RECENT_MESSAGES + 2:
            return messages

        # 分离：旧消息（需要压缩） + 新消息（保留）
        recent = messages[-KEEP_RECENT_MESSAGES:]
        old = messages[:-KEEP_RECENT_MESSAGES]

        serialized = [s for s in (serialize_message(m) for m in old) if s]
        old_text = "\n\n".join(serialized)

        if not old_text.strip():
            return messages

        try:
            # 构建摘要提示词（含上一次摘要则更新，否则新建）
            if self._last_summary:
                summary_prompt = f"""更新以下摘要，合并新的对话历史。保留仍然正确的细节，删除过时的信息。

上一次摘要：
{self._last_summary}

新的对话历史：
{old_text}

{SUMMARY_TEMPLATE}"""
            else:
                summary_prompt = f"""生成结构化摘要，包含：决策、工具结果、关键信息。

{SUMMARY_TEMPLATE}

对话历史：
{old_text}"""

            resp = await llm.generate(
                "你是上下文压缩器。只输出结构化摘要，不要其他文字。",
                [{"role": "user", "content": summary_prompt}],
                temperature=0.1,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            summary = resp.content.strip()

            if summary:
                self._last_summary = summary
                return [
                    {"role": "system", "content": f"[会话历史摘要]\n{summary}"},
                    *recent,
                ]
        except asyncio.CancelledError:
            raise
        except (RuntimeError, httpx.HTTPError) as e:
            logging.warning(f"Level 4 auto_compact LLM 调用失败: {e}", exc_info=True)

        # fallback 到 Level 2 的 compact_if_needed
        return await self.compact_if_needed(messages, system_prompt)

    # ── Level 5: budget_reduce ──────────────────────────────────

    def budget_reduce(self, messages: list[dict], budget_tokens: int) -> list[dict]:
        """Level 5: 按 token 预算硬裁剪

        - 从最早的消息开始裁剪（但保留 system 摘要 + 最近 N 条）
        - 每条消息超预算则截断 content，仍超则删除该消息（但必须保持 tool_calls 配对）
        - 用 estimate_messages_tokens 计算总量

        Args:
            messages: 当前对话消息列表
            budget_tokens: token 预算上限

        Returns:
            裁剪后的消息列表
        """
        if not messages:
            return messages

        total = estimate_messages_tokens(messages)
        if total <= budget_tokens:
            return messages

        result = list(messages)

        # 保留：开头的 system 摘要消息 + 末尾 KEEP_RECENT_MESSAGES 条
        sys_count = 0
        for m in result:
            if m.get("role") == "system":
                sys_count += 1
            else:
                break

        # 1. 截断过长消息的 content（非 system、非最近 N 条）
        recent_start = max(sys_count, len(result) - KEEP_RECENT_MESSAGES)
        per_msg_budget = max(500, budget_tokens // 20)

        for i in range(sys_count, recent_start):
            msg = result[i]
            content = msg.get("content", "")
            if isinstance(content, str) and estimate_tokens(content) > per_msg_budget:
                truncated = content[:per_msg_budget] + "...[Level5 裁剪]"
                result[i] = {**msg, "content": truncated}

        # 2. 从最早开始删除消息（保持 tool_calls 配对）
        i = sys_count
        while (i < len(result) - KEEP_RECENT_MESSAGES
               and estimate_messages_tokens(result) > budget_tokens):
            msg = result[i]
            role = msg.get("role")

            if role == "assistant" and msg.get("tool_calls"):
                # 删除 assistant + 其所有 tool 结果（保持配对完整）
                tool_call_ids = {tc.get("id") for tc in msg["tool_calls"]}
                end = i + 1
                while (end < len(result)
                       and result[end].get("role") == "tool"
                       and result[end].get("tool_call_id") in tool_call_ids):
                    end += 1
                del result[i:end]
                # 不增加 i（下一个元素移到当前位置）
                continue

            if role == "tool":
                # 不单独删除 tool（会破坏配对），跳过
                i += 1
                continue

            # user / 纯 assistant / system（非开头）：可删除
            del result[i]
            # 不增加 i（下一个元素移到当前位置）

        return result

    # ── 5 层压缩管道 ─────────────────────────────────────────────

    async def compact_pipeline(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """5 层压缩管道：Level 1→2→3→4→5 顺序执行

        每层检查是否需要（超阈值才触发），避免无谓计算。
        - Level 1: microcompact（实时轻量，不调 LLM）
        - Level 2: compact_if_needed（超阈值时 LLM 摘要）
        - Level 3: context_collapse（合并连续同类工具结果）
        - Level 4: auto_compact（小模型摘要，超 0.9 阈值）
        - Level 5: budget_reduce（按预算硬裁剪）

        Args:
            messages: 当前对话消息列表
            system_prompt: 系统提示词

        Returns:
            压缩后的消息列表
        """
        # Level 1: microcompact（实时轻量压缩，不调 LLM）
        messages = self.microcompact(messages)

        # Level 2: compact_if_needed（超阈值时调 LLM 生成摘要）
        messages = await self.compact_if_needed(messages, system_prompt)

        # Level 3: context_collapse（合并连续同类工具结果）
        messages = self.context_collapse(messages)

        # Level 4: auto_compact（小模型摘要，超 0.9 阈值）
        messages = await self.auto_compact(messages, system_prompt)

        # Level 5: budget_reduce（按预算硬裁剪）
        budget = self._get_context_limit()
        messages = self.budget_reduce(messages, budget)

        return messages
