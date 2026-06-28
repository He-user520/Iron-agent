"""Stop Hooks — 收敛检测器

在 Agent 循环中检测无效循环，主动终止避免浪费 token / 时间。

设计参考 Claude Code 的 stop_reason 机制：
- 多个 StopHook 串联，任一触发即停止
- 每个 Hook 维护自己的内部状态（跨步骤累计）
- StopHookManager 统一调度，支持 enable/disable 开关
- 每次 process() 开始时调用 reset() 清理状态

内置 Hook：
1. MaxConsecutiveFailures — 连续 N 次工具失败（默认 5）
2. DoomLoopDetector — 同一工具+参数+结果连续 3 次相同（扩展 engine._check_doom_loop）
3. MaxToolRepetition — 同一工具名连续调用超过 N 次（默认 10）
4. NoProgressDetector — 连续 N 步工具结果无新信息（默认 8）
"""
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StopDecision:
    """停止决策

    Attributes:
        should_stop: 是否停止循环
        reason: 停止原因（人类可读，用于 UI 展示）
        severity: 严重等级 "info"/"warning"/"error"
    """
    should_stop: bool
    reason: str = ""
    severity: str = "info"


class StopHook(ABC):
    """StopHook 抽象基类

    子类需实现 check() 方法，返回 StopDecision。
    可选实现 reset() 方法清理内部状态（每次新会话开始时调用）。
    """

    @abstractmethod
    def check(self, tool_calls: list[dict], tool_results: list[dict],
              step: int, recent_calls: list) -> StopDecision:
        """检查是否应该停止循环

        Args:
            tool_calls: 本步骤的 tool 调用列表 [{name, arguments, id}, ...]
            tool_results: 本步骤的 tool 执行结果 [{tool_call_id, role, content}, ...]
            step: 当前步数（0-based）
            recent_calls: engine 的最近调用签名列表（参考用，可为空）

        Returns:
            StopDecision — should_stop=True 时立即终止循环
        """

    def reset(self) -> None:
        """重置内部状态（新会话开始时调用）"""
        pass


# ── 辅助函数 ──────────────────────────────────────────────────

def _is_failed_result(result: dict) -> bool:
    """判断单个 tool_result 是否表示失败

    tool_result.content 是 JSON 字符串，解析后含 success 字段。
    解析失败或非 dict 视为非失败（保守判断，避免误触发）。
    """
    content = result.get("content")
    if not isinstance(content, str) or not content:
        return False
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("success") is False


def _signature(value) -> str:
    """生成稳定的签名（用于去重/比较）

    截断到 200 字符，与 engine._check_doom_loop 的签名长度一致，
    避免长参数导致内存膨胀。
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)[:200]
    except (TypeError, ValueError):
        return str(value)[:200]


# ── 内置 StopHook 实现 ────────────────────────────────────────

class MaxConsecutiveFailures(StopHook):
    """连续 N 次工具失败则停止

    统计连续失败的 tool_result 数量（跨步骤累计），
    任一成功结果即重置计数。
    """

    def __init__(self, max_failures: int = 5):
        self.max_failures = max_failures
        self._consecutive_failures = 0

    def check(self, tool_calls: list[dict], tool_results: list[dict],
              step: int, recent_calls: list) -> StopDecision:
        for result in tool_results or []:
            if _is_failed_result(result):
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.max_failures:
                    return StopDecision(
                        should_stop=True,
                        reason=f"连续 {self._consecutive_failures} 次工具失败，"
                               f"达到上限 {self.max_failures}",
                        severity="error",
                    )
            else:
                # 成功 → 重置计数（即使是第一次也重置为 0，确保只有"连续"失败才计数）
                self._consecutive_failures = 0
        return StopDecision(should_stop=False)

    def reset(self) -> None:
        self._consecutive_failures = 0


class DoomLoopDetector(StopHook):
    """同一工具+参数+结果连续 3 次相同则停止

    扩展 engine._check_doom_loop：后者仅检测调用签名（name+args），
    本 Hook 额外比对执行结果，识别"参数变化但结果相同"的隐性循环。

    触发阈值固定为 3（与 engine._check_doom_loop 的连续检测一致）。
    """

    TRIGGER_COUNT = 3

    def __init__(self):
        self._last_signature: Optional[str] = None
        self._repeat_count = 0

    def check(self, tool_calls: list[dict], tool_results: list[dict],
              step: int, recent_calls: list) -> StopDecision:
        # 构建 (call, result) 配对（按 tool_call_id 关联）
        pairs = self._pair_calls_results(tool_calls, tool_results)
        for call, result in pairs:
            sig = self._build_signature(call, result)
            if sig == self._last_signature:
                self._repeat_count += 1
            else:
                self._last_signature = sig
                self._repeat_count = 1
            if self._repeat_count >= self.TRIGGER_COUNT:
                name = call.get("name", "<unknown>")
                return StopDecision(
                    should_stop=True,
                    reason=f"检测到 doom_loop：工具 {name} 连续 "
                           f"{self._repeat_count} 次相同调用+结果",
                    severity="warning",
                )
        return StopDecision(should_stop=False)

    @staticmethod
    def _pair_calls_results(tool_calls: list[dict],
                            tool_results: list[dict]) -> list[tuple]:
        """按 tool_call_id 配对 call 和 result

        无 id 的 result 不参与配对（如 system 注入的提示消息）。
        """
        if not tool_calls or not tool_results:
            return []
        result_map: dict = {}
        for r in tool_results or []:
            cid = r.get("tool_call_id", "")
            if cid:
                result_map[cid] = r
        pairs = []
        for call in tool_calls or []:
            cid = call.get("id", "")
            result = result_map.get(cid)
            if result is not None:
                pairs.append((call, result))
        return pairs

    @staticmethod
    def _build_signature(call: dict, result: dict) -> str:
        """构建调用+结果的复合签名"""
        name = call.get("name", "")
        args = call.get("arguments", {}) or {}
        content = result.get("content", "") or ""
        return f"{name}:{_signature(args)}:{_signature(content)}"

    def reset(self) -> None:
        self._last_signature = None
        self._repeat_count = 0


class MaxToolRepetition(StopHook):
    """同一工具名连续调用超过 N 次则停止

    统计跨步骤的连续同名调用，换工具名即重置计数。
    单步内多个同名调用也累计计数。
    """

    def __init__(self, max_repetition: int = 10):
        self.max_repetition = max_repetition
        self._last_tool: str = ""
        self._count = 0

    def check(self, tool_calls: list[dict], tool_results: list[dict],
              step: int, recent_calls: list) -> StopDecision:
        for call in tool_calls or []:
            name = call.get("name", "")
            if name and name == self._last_tool:
                self._count += 1
            else:
                self._last_tool = name
                self._count = 1
            if self._count >= self.max_repetition:
                return StopDecision(
                    should_stop=True,
                    reason=f"工具 {name} 连续调用 {self._count} 次，"
                           f"超过上限 {self.max_repetition}",
                    severity="warning",
                )
        return StopDecision(should_stop=False)

    def reset(self) -> None:
        self._last_tool = ""
        self._count = 0


class NoProgressDetector(StopHook):
    """连续 N 步工具结果无新信息则停止

    维护已见过的 result content 签名集合：
    - 若某步所有结果都已见过 → 无进展 → 计数+1
    - 若有任一新结果 → 重置计数
    - 计数达到 N → 触发停止

    首次见到结果算"有进展"（计数为 0），后续重复才算"无进展"。
    """

    def __init__(self, max_no_progress: int = 8):
        self.max_no_progress = max_no_progress
        self._seen_result_sigs: set = set()
        self._no_progress_steps = 0

    def check(self, tool_calls: list[dict], tool_results: list[dict],
              step: int, recent_calls: list) -> StopDecision:
        if not tool_results:
            # 没有工具结果（如 chat 终止步）→ 不算无进展，保持现状
            return StopDecision(should_stop=False)
        all_seen = True
        for result in tool_results:
            content = result.get("content", "") or ""
            sig = _signature(content)
            if sig not in self._seen_result_sigs:
                all_seen = False
                self._seen_result_sigs.add(sig)
        if all_seen:
            self._no_progress_steps += 1
            if self._no_progress_steps >= self.max_no_progress:
                return StopDecision(
                    should_stop=True,
                    reason=f"连续 {self._no_progress_steps} 步工具结果无新信息，"
                           f"疑似陷入无效循环",
                    severity="warning",
                )
        else:
            self._no_progress_steps = 0
        return StopDecision(should_stop=False)

    def reset(self) -> None:
        self._seen_result_sigs.clear()
        self._no_progress_steps = 0


# ── StopHook 管理器 ──────────────────────────────────────────

class StopHookManager:
    """StopHook 管理器

    - 注册多个 StopHook
    - 按注册顺序检查，任一触发即返回该 StopDecision
    - 支持 enabled 开关（False 时全部跳过，用于配置禁用）
    - reset() 重置所有 hook 的内部状态（每次 process() 开始时调用）
    """

    def __init__(self, enabled: bool = True):
        self._hooks: list[StopHook] = []
        self._enabled = enabled

    def register(self, hook: StopHook) -> None:
        """注册一个 StopHook（追加到末尾，按顺序检查）"""
        self._hooks.append(hook)

    def check_all(self, tool_calls: list[dict], tool_results: list[dict],
                  step: int, recent_calls: list) -> Optional[StopDecision]:
        """按顺序检查所有 hook，返回第一个触发的 StopDecision

        Returns:
            StopDecision（should_stop=True）或 None（无触发 / 已禁用）
        """
        if not self._enabled:
            return None
        for hook in self._hooks:
            try:
                decision = hook.check(tool_calls, tool_results, step, recent_calls)
            except (TypeError, ValueError, AttributeError, KeyError) as e:
                # 单个 hook 异常不影响其他 hook 检查（fail-open，不误停）
                logger.warning("StopHook %s 检查异常: %s",
                               type(hook).__name__, e, exc_info=True)
                continue
            if decision and decision.should_stop:
                return decision
        return None

    def reset(self) -> None:
        """重置所有 hook 的内部状态（新会话开始时调用）"""
        for hook in self._hooks:
            try:
                hook.reset()
            except (TypeError, ValueError, AttributeError) as e:
                logger.warning("StopHook %s reset 异常: %s",
                               type(hook).__name__, e, exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @property
    def hooks(self) -> list:
        """已注册的 hook 列表（只读视图）"""
        return list(self._hooks)
