"""Stop Hooks 单元测试 — P1-2 收敛检测器

覆盖 4 个内置 StopHook + StopHookManager 调度逻辑 + 边界情况。

运行方式: pytest tests/test_stop_hooks.py -v
"""
import json

import pytest

from iron.agent.stop_hooks import (
    StopDecision,
    StopHook,
    StopHookManager,
    MaxConsecutiveFailures,
    DoomLoopDetector,
    MaxToolRepetition,
    NoProgressDetector,
)


# ── 测试辅助 ──────────────────────────────────────────────────

def _failed_result(call_id: str = "1", error: str = "test error") -> dict:
    """构造一个失败的 tool_result"""
    return {
        "tool_call_id": call_id,
        "role": "tool",
        "content": json.dumps({"success": False, "error": error}, ensure_ascii=False),
    }


def _success_result(call_id: str = "1", content: str = "ok") -> dict:
    """构造一个成功的 tool_result"""
    return {
        "tool_call_id": call_id,
        "role": "tool",
        "content": json.dumps({"success": True, "content": content}, ensure_ascii=False),
    }


def _call(name: str = "read_file", args: dict = None, call_id: str = "1") -> dict:
    """构造一个 tool_call"""
    return {
        "id": call_id,
        "name": name,
        "arguments": args or {"path": "a.c"},
    }


# ── 1. MaxConsecutiveFailures 测试 ────────────────────────────

class TestMaxConsecutiveFailures:
    """连续工具失败检测"""

    def test_max_consecutive_failures(self):
        """连续 5 次失败触发（默认阈值 5）"""
        hook = MaxConsecutiveFailures(max_failures=5)
        call = _call()
        result = _failed_result()
        # 前 4 次不触发
        for i in range(4):
            decision = hook.check([call], [result], i, [])
            assert decision.should_stop is False, f"第 {i + 1} 次不应触发"
        # 第 5 次触发
        decision = hook.check([call], [result], 4, [])
        assert decision.should_stop is True
        assert decision.severity == "error"
        assert "5" in decision.reason

    def test_max_consecutive_failures_reset(self):
        """成功后计数重置"""
        hook = MaxConsecutiveFailures(max_failures=5)
        call = _call()
        failed = _failed_result()
        success = _success_result()
        # 4 次失败（未达阈值）
        for i in range(4):
            decision = hook.check([call], [failed], i, [])
            assert decision.should_stop is False
        # 1 次成功 → 重置计数
        decision = hook.check([call], [success], 4, [])
        assert decision.should_stop is False
        # 再 4 次失败（计数从 0 重新累计，4 < 5 不触发）
        for i in range(5, 9):
            decision = hook.check([call], [failed], i, [])
            assert decision.should_stop is False, f"重置后第 {i - 4} 次不应触发"
        # 第 5 次失败（计数到 5）才触发
        decision = hook.check([call], [failed], 9, [])
        assert decision.should_stop is True

    def test_max_consecutive_failures_empty_results(self):
        """空 tool_results 不触发"""
        hook = MaxConsecutiveFailures(max_failures=3)
        decision = hook.check([], [], 0, [])
        assert decision.should_stop is False

    def test_max_consecutive_failures_reset_method(self):
        """reset() 方法清零计数"""
        hook = MaxConsecutiveFailures(max_failures=3)
        call = _call()
        result = _failed_result()
        hook.check([call], [result], 0, [])
        hook.check([call], [result], 1, [])
        assert hook._consecutive_failures == 2
        hook.reset()
        assert hook._consecutive_failures == 0


# ── 2. DoomLoopDetector 测试 ──────────────────────────────────

class TestDoomLoopDetector:
    """同一工具+参数+结果连续 3 次相同检测"""

    def test_doom_loop_detector(self):
        """3 次相同调用+结果触发"""
        hook = DoomLoopDetector()
        call = _call("read_file", {"path": "a.c"}, "1")
        result = _success_result("1", "hello")
        # 前 2 次不触发
        decision = hook.check([call], [result], 0, [])
        assert decision.should_stop is False
        decision = hook.check([call], [result], 1, [])
        assert decision.should_stop is False
        # 第 3 次触发
        decision = hook.check([call], [result], 2, [])
        assert decision.should_stop is True
        assert decision.severity == "warning"
        assert "doom_loop" in decision.reason

    def test_doom_loop_different_args(self):
        """不同参数不触发"""
        hook = DoomLoopDetector()
        result = _success_result("1", "hello")
        # 3 次不同参数
        for path in ["a.c", "b.c", "c.c"]:
            call = _call("read_file", {"path": path}, "1")
            decision = hook.check([call], [result], 0, [])
            assert decision.should_stop is False, f"path={path} 不应触发"

    def test_doom_loop_different_results(self):
        """相同调用但不同结果不触发"""
        hook = DoomLoopDetector()
        call = _call("read_file", {"path": "a.c"}, "1")
        # 3 次相同调用但结果不同
        for content in ["aaa", "bbb", "ccc"]:
            result = _success_result("1", content)
            decision = hook.check([call], [result], 0, [])
            assert decision.should_stop is False, f"content={content} 不应触发"

    def test_doom_loop_empty_inputs(self):
        """空 tool_calls 或 tool_results 不触发"""
        hook = DoomLoopDetector()
        assert hook.check([], [], 0, []).should_stop is False
        assert hook.check([_call()], [], 0, []).should_stop is False
        assert hook.check([], [_success_result()], 0, []).should_stop is False


# ── 3. MaxToolRepetition 测试 ─────────────────────────────────

class TestMaxToolRepetition:
    """同一工具名连续调用检测"""

    def test_max_tool_repetition(self):
        """连续 10 次同工具触发（默认阈值 10）"""
        hook = MaxToolRepetition(max_repetition=10)
        call = _call("read_file")
        # 前 9 次不触发
        for i in range(9):
            decision = hook.check([call], [_success_result()], i, [])
            assert decision.should_stop is False, f"第 {i + 1} 次不应触发"
        # 第 10 次触发
        decision = hook.check([call], [_success_result()], 9, [])
        assert decision.should_stop is True
        assert decision.severity == "warning"
        assert "read_file" in decision.reason

    def test_max_tool_repetition_different_tools(self):
        """换工具名重置计数"""
        hook = MaxToolRepetition(max_repetition=5)
        # 4 次 read_file
        for i in range(4):
            decision = hook.check([_call("read_file")], [_success_result()], i, [])
            assert decision.should_stop is False
        # 换 write_file → 重置
        decision = hook.check([_call("write_file")], [_success_result()], 4, [])
        assert decision.should_stop is False
        # 再 4 次 read_file 不触发（计数从 1 重新开始）
        for i in range(5, 9):
            decision = hook.check([_call("read_file")], [_success_result()], i, [])
            assert decision.should_stop is False

    def test_max_tool_repetition_multiple_in_one_step(self):
        """单步多个同名调用累计计数"""
        hook = MaxToolRepetition(max_repetition=3)
        # 一步内 3 个同名调用 → 触发
        calls = [_call("read_file", call_id=str(i)) for i in range(3)]
        results = [_success_result(str(i)) for i in range(3)]
        decision = hook.check(calls, results, 0, [])
        assert decision.should_stop is True


# ── 4. NoProgressDetector 测试 ────────────────────────────────

class TestNoProgressDetector:
    """无新信息检测"""

    def test_no_progress_detector(self):
        """8 步无新信息触发（默认阈值 8）"""
        hook = NoProgressDetector(max_no_progress=8)
        call = _call()
        result = _success_result(content="same content")
        # 第 1 次：新信息，不触发，计数为 0
        decision = hook.check([call], [result], 0, [])
        assert decision.should_stop is False
        # 接下来 7 次无新信息，仍不触发
        for i in range(1, 8):
            decision = hook.check([call], [result], i, [])
            assert decision.should_stop is False, f"第 {i + 1} 步不应触发"
        # 第 8 次无新信息（计数到 8）触发
        decision = hook.check([call], [result], 8, [])
        assert decision.should_stop is True
        assert decision.severity == "warning"
        assert "8" in decision.reason

    def test_no_progress_reset_on_new_info(self):
        """出现新信息重置计数"""
        hook = NoProgressDetector(max_no_progress=3)
        call = _call()
        # 第 1 次：新信息
        hook.check([call], [_success_result(content="A")], 0, [])
        # 第 2 次：无新信息（计数=1）
        hook.check([call], [_success_result(content="A")], 1, [])
        # 第 3 次：新信息 B → 重置计数
        decision = hook.check([call], [_success_result(content="B")], 2, [])
        assert decision.should_stop is False
        # 再 2 次无新信息（计数=2，未达 3）
        hook.check([call], [_success_result(content="B")], 3, [])
        decision = hook.check([call], [_success_result(content="B")], 4, [])
        assert decision.should_stop is False
        # 第 3 次无新信息 → 触发
        decision = hook.check([call], [_success_result(content="B")], 5, [])
        assert decision.should_stop is True

    def test_no_progress_empty_results(self):
        """空 tool_results 不触发（如 chat 终止步）"""
        hook = NoProgressDetector(max_no_progress=2)
        decision = hook.check([], [], 0, [])
        assert decision.should_stop is False


# ── 5. StopHookManager 测试 ──────────────────────────────────

class _AlwaysStop(StopHook):
    """总是触发的测试 hook"""

    def __init__(self, name: str = "always"):
        self.name = name

    def check(self, tool_calls, tool_results, step, recent_calls):
        return StopDecision(should_stop=True, reason=f"{self.name} triggered",
                            severity="warning")


class _NeverStop(StopHook):
    """从不触发的测试 hook"""

    def check(self, tool_calls, tool_results, step, recent_calls):
        return StopDecision(should_stop=False)


class TestStopHookManager:
    """StopHookManager 调度逻辑"""

    def test_stop_hook_manager_priority(self):
        """多 hook 注册，按顺序检查，第一个触发的胜出"""
        manager = StopHookManager(enabled=True)
        # 先注册 NeverStop，再注册 AlwaysStop
        manager.register(_NeverStop())
        manager.register(_AlwaysStop(name="second"))
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is not None
        assert decision.should_stop is True
        assert "second" in decision.reason

    def test_stop_hook_manager_first_wins(self):
        """第一个触发的 hook 胜出，后续不执行"""
        manager = StopHookManager(enabled=True)
        manager.register(_AlwaysStop(name="first"))
        manager.register(_AlwaysStop(name="second"))
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is not None
        assert "first" in decision.reason
        assert "second" not in decision.reason

    def test_stop_hook_manager_none_trigger(self):
        """所有 hook 都不触发 → 返回 None"""
        manager = StopHookManager(enabled=True)
        manager.register(_NeverStop())
        manager.register(_NeverStop())
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is None

    def test_stop_hook_disabled(self):
        """stop_hooks_enabled=False 时全部跳过"""
        manager = StopHookManager(enabled=False)
        manager.register(_AlwaysStop(name="always"))
        # 即使 AlwaysStop 也不触发
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is None

    def test_stop_hook_manager_reset(self):
        """reset() 清空所有 hook 状态"""
        manager = StopHookManager(enabled=True)
        failures = MaxConsecutiveFailures(max_failures=3)
        manager.register(failures)
        # 累计 2 次失败
        manager.check_all([_call()], [_failed_result()], 0, [])
        manager.check_all([_call()], [_failed_result()], 1, [])
        assert failures._consecutive_failures == 2
        # reset 后清零
        manager.reset()
        assert failures._consecutive_failures == 0

    def test_stop_hook_manager_enable_toggle(self):
        """enabled 属性可动态切换"""
        manager = StopHookManager(enabled=True)
        assert manager.enabled is True
        manager.enabled = False
        assert manager.enabled is False
        manager.register(_AlwaysStop())
        assert manager.check_all([_call()], [_success_result()], 0, []) is None
        manager.enabled = True
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is not None


# ── 6. 边界情况测试 ───────────────────────────────────────────

class TestStopHooksEdgeCases:
    """边界情况：空列表、None、异常输入"""

    def test_empty_tool_calls_all_hooks(self):
        """空 tool_calls 对所有 hook 不触发"""
        hooks = [
            MaxConsecutiveFailures(3),
            DoomLoopDetector(),
            MaxToolRepetition(3),
            NoProgressDetector(3),
        ]
        for hook in hooks:
            decision = hook.check([], [], 0, [])
            assert decision.should_stop is False, f"{type(hook).__name__} 空输入不应触发"

    def test_none_recent_calls(self):
        """recent_calls 为 None 不崩溃"""
        hook = MaxConsecutiveFailures(3)
        # recent_calls 传 None（虽然类型提示是 list，做防御性测试）
        decision = hook.check([_call()], [_success_result()], 0, None)
        assert decision.should_stop is False

    def test_manager_with_no_hooks(self):
        """空 manager（未注册任何 hook）返回 None"""
        manager = StopHookManager(enabled=True)
        decision = manager.check_all([_call()], [_success_result()], 0, [])
        assert decision is None

    def test_non_json_content_treated_as_success(self):
        """非 JSON content 不被误判为失败"""
        hook = MaxConsecutiveFailures(max_failures=2)
        result = {"tool_call_id": "1", "role": "tool", "content": "not json"}
        decision = hook.check([_call()], [result], 0, [])
        assert decision.should_stop is False

    def test_stop_decision_defaults(self):
        """StopDecision 默认值"""
        d = StopDecision(should_stop=False)
        assert d.reason == ""
        assert d.severity == "info"
