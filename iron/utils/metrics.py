"""观测性指标采集器

不引入外部依赖，内存存储，会话级。

用法:
    from iron.utils.metrics import counter, gauge, timing, get_summary
    counter("tool_calls", tags={"tool": "edit_file"})
    gauge("context_tokens", 5000)
    timing("llm_response", 2.5)
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MetricEntry:
    """单个指标条目"""
    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    tags: dict = field(default_factory=dict)


class MetricsCollector:
    """指标采集器（线程安全单例）

    用法:
        MetricsCollector().counter("tool_calls", tags={"tool": "edit_file"})
        MetricsCollector().gauge("context_tokens", 5000)
        MetricsCollector().timing("llm_response", 2.5)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._counters = defaultdict(float)
        self._gauges = {}
        self._timings = defaultdict(list)
        self._lock_data = threading.Lock()

    def counter(self, name: str, value: float = 1, tags: dict = None) -> None:
        """递增计数器"""
        with self._lock_data:
            key = self._key(name, tags)
            self._counters[key] += value

    def gauge(self, name: str, value: float, tags: dict = None) -> None:
        """设置 gauge 值"""
        with self._lock_data:
            key = self._key(name, tags)
            self._gauges[key] = value

    def timing(self, name: str, seconds: float, tags: dict = None) -> None:
        """记录耗时"""
        with self._lock_data:
            key = self._key(name, tags)
            self._timings[key].append(seconds)
            # 只保留最近 100 个采样
            if len(self._timings[key]) > 100:
                self._timings[key] = self._timings[key][-100:]

    def get_summary(self) -> dict:
        """获取指标摘要"""
        with self._lock_data:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "timings": {
                    k: {
                        "count": len(v),
                        "avg": sum(v) / len(v) if v else 0,
                        "min": min(v) if v else 0,
                        "max": max(v) if v else 0,
                    }
                    for k, v in self._timings.items()
                },
            }

    def reset(self) -> None:
        """重置所有指标"""
        with self._lock_data:
            self._counters.clear()
            self._gauges.clear()
            self._timings.clear()

    def _key(self, name: str, tags: dict = None) -> str:
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}|{tag_str}"


# 全局单例便捷函数
def counter(name: str, value: float = 1, tags: dict = None) -> None:
    MetricsCollector().counter(name, value, tags)


def gauge(name: str, value: float, tags: dict = None) -> None:
    MetricsCollector().gauge(name, value, tags)


def timing(name: str, seconds: float, tags: dict = None) -> None:
    MetricsCollector().timing(name, seconds, tags)


def get_summary() -> dict:
    return MetricsCollector().get_summary()


def reset() -> None:
    """重置全局单例指标（便捷函数）"""
    MetricsCollector().reset()
