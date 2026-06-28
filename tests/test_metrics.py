"""Track 9 Step 7: MetricsCollector 单元测试

验证：
- counter 递增
- gauge 设置
- timing 记录 + 统计
- 线程安全
- reset 清空
- 单例
- tags 区分
- get_summary 结构
"""
import threading
import time

import pytest

from iron.utils.metrics import (
    MetricsCollector,
    counter,
    gauge,
    timing,
    get_summary,
    reset,
)


@pytest.fixture(autouse=True)
def _reset_metrics_before_each():
    """每个测试前重置单例，避免测试间污染"""
    reset()
    yield
    reset()


def test_counter_increments():
    """counter 默认 +1，可指定 value 累加"""
    MetricsCollector().counter("tool_calls")
    MetricsCollector().counter("tool_calls")
    MetricsCollector().counter("tool_calls", value=5)
    summary = get_summary()
    assert summary["counters"]["tool_calls"] == 7.0


def test_gauge_sets_value():
    """gauge 设置为最新值（覆盖）"""
    MetricsCollector().gauge("context_tokens", 1000)
    MetricsCollector().gauge("context_tokens", 5000)
    summary = get_summary()
    assert summary["gauges"]["context_tokens"] == 5000


def test_timing_records_and_stats():
    """timing 记录多次，统计 count/avg/min/max"""
    m = MetricsCollector()
    m.timing("llm_response", 1.0)
    m.timing("llm_response", 2.0)
    m.timing("llm_response", 3.0)
    summary = get_summary()
    stats = summary["timings"]["llm_response"]
    assert stats["count"] == 3
    assert stats["min"] == 1.0
    assert stats["max"] == 3.0
    assert stats["avg"] == pytest.approx(2.0)


def test_reset_clears_all():
    """reset 清空 counters/gauges/timings"""
    m = MetricsCollector()
    m.counter("a")
    m.gauge("b", 1)
    m.timing("c", 0.5)
    m.reset()
    summary = get_summary()
    assert summary["counters"] == {}
    assert summary["gauges"] == {}
    assert summary["timings"] == {}


def test_singleton_identity():
    """MetricsCollector 是单例，多次构造返回同一实例"""
    a = MetricsCollector()
    b = MetricsCollector()
    assert a is b


def test_tags_distinguish_keys():
    """相同 name 不同 tags 视为不同条目"""
    m = MetricsCollector()
    m.counter("tool_calls", tags={"tool": "edit_file"})
    m.counter("tool_calls", tags={"tool": "read_file"})
    m.counter("tool_calls", tags={"tool": "edit_file"})
    summary = get_summary()
    keys = summary["counters"].keys()
    assert any("tool=edit_file" in k for k in keys)
    assert any("tool=read_file" in k for k in keys)
    # edit_file 调用 2 次，read_file 调用 1 次
    edit_val = [v for k, v in summary["counters"].items() if "tool=edit_file" in k][0]
    read_val = [v for k, v in summary["counters"].items() if "tool=read_file" in k][0]
    assert edit_val == 2.0
    assert read_val == 1.0


def test_get_summary_structure():
    """get_summary 返回 dict 含 counters/gauges/timings 三个键"""
    summary = get_summary()
    assert isinstance(summary, dict)
    assert set(summary.keys()) == {"counters", "gauges", "timings"}
    assert isinstance(summary["counters"], dict)
    assert isinstance(summary["gauges"], dict)
    assert isinstance(summary["timings"], dict)


def test_thread_safety_under_concurrent_writes():
    """多线程并发 counter 不丢更新"""
    m = MetricsCollector()
    n_threads = 8
    n_per_thread = 1000

    def _worker():
        for _ in range(n_per_thread):
            m.counter("concurrent_counter")

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = get_summary()
    expected = n_threads * n_per_thread
    assert summary["counters"]["concurrent_counter"] == float(expected)


def test_global_helper_functions():
    """模块级便捷函数 counter/gauge/timing/get_summary 走同一单例"""
    counter("g_counter", 3)
    gauge("g_gauge", 42)
    timing("g_timing", 0.25)
    summary = get_summary()
    assert summary["counters"]["g_counter"] == 3.0
    assert summary["gauges"]["g_gauge"] == 42
    assert summary["timings"]["g_timing"]["count"] == 1
    assert summary["timings"]["g_timing"]["avg"] == pytest.approx(0.25)


def test_timing_caps_at_100_samples():
    """timing 仅保留最近 100 个采样，防止内存增长"""
    m = MetricsCollector()
    for i in range(150):
        m.timing("capped", float(i))
    summary = get_summary()
    stats = summary["timings"]["capped"]
    assert stats["count"] == 100
    # 保留的是最后 100 个（i=50..149），min=50, max=149
    assert stats["min"] == 50.0
    assert stats["max"] == 149.0
