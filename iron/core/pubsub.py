"""PubSub 事件总线 — 泛型事件分发

参考 OpenCode 的 events 模块设计：
- 类型安全：用泛型 T 约束事件类型
- 异步优先：subscribe 自动识别 async 回调
- 同步兼容：subscribe_sync 显式订阅同步回调（自动包装为 async）
- 错误隔离：单个监听者异常不影响其他订阅者
- 并行通知：用 asyncio.gather 并行调用所有订阅者

用法:
    bus = EventBus()
    bus.subscribe("tool.executed", my_async_handler)
    bus.subscribe_sync("chat.response", my_sync_handler)
    await bus.publish(Event("tool.executed", {"name": "read_file"}))
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class Event:
    """事件基类 — 所有发布到总线的事件载体

    Attributes:
        type: 事件类型字符串（如 "tool.executed"、"chat.response"）
        data: 事件负载数据（自由 dict 结构）
        timestamp: 事件创建时间戳（默认取 now()）
    """
    type: str
    data: dict
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Subscription:
    """订阅信息 — 描述一个回调订阅

    Attributes:
        id: 订阅唯一 ID（用于取消订阅）
        event_type: 订阅的事件类型
        callback: 回调函数（async 或 sync）
        is_async: 回调是否为 async 函数
    """
    id: int
    event_type: str
    callback: Callable
    is_async: bool


class EventBus:
    """事件总线 — 多对多事件分发

    支持:
    - 多个订阅者订阅同一事件类型
    - 一个订阅者订阅多个事件类型
    - async 和 sync 回调混合
    - 错误隔离：单个订阅者异常不影响其他
    - 并行通知：用 asyncio.gather 并行调用

    线程安全说明:
    - subscribe/unsubscribe/clear 是同步方法，应在事件循环同一线程调用
    - publish 是 async 方法，内部用 asyncio.gather 并行通知
    """

    def __init__(self):
        # 用 defaultdict 避免每个事件类型手动初始化
        self._subscribers: dict[str, list[Subscription]] = defaultdict(list)
        # 订阅 ID 自增计数器（从 1 开始，0 表示无效）
        self._next_id = 0
        # 异步锁（保留以备未来扩展为跨线程订阅场景；当前同步方法不强制加锁）
        self._lock = asyncio.Lock()

    def subscribe(self, event_type: str, callback: Callable) -> int:
        """订阅事件，返回 subscription_id 用于取消

        自动检测 callback 是否为 async 函数：
        - async 函数：publish 时直接 await
        - sync 函数：publish 时用 asyncio.to_thread 包装，避免阻塞事件循环

        Args:
            event_type: 事件类型字符串
            callback: 回调函数，签名为 callback(event: Event) -> None | Awaitable

        Returns:
            订阅 ID（>0），可用于 unsubscribe
        """
        self._next_id += 1
        sub_id = self._next_id
        # asyncio.iscoroutinefunction 能正确识别 async def 定义的函数
        is_async = asyncio.iscoroutinefunction(callback)
        sub = Subscription(
            id=sub_id,
            event_type=event_type,
            callback=callback,
            is_async=is_async,
        )
        self._subscribers[event_type].append(sub)
        return sub_id

    def subscribe_sync(self, event_type: str, callback: Callable) -> int:
        """订阅同步回调（语义清晰版本，自动包装为 async）

        与 subscribe() 行为一致（subscribe 也会自动检测），
        显式方法用于代码可读性，明确告诉调用者这是同步回调。

        Args:
            event_type: 事件类型字符串
            callback: 同步回调函数 callback(event: Event) -> None

        Returns:
            订阅 ID（>0），可用于 unsubscribe
        """
        # 复用 subscribe 的检测逻辑，避免重复实现
        return self.subscribe(event_type, callback)

    def unsubscribe(self, subscription_id: int) -> bool:
        """取消订阅

        Args:
            subscription_id: subscribe() 返回的订阅 ID

        Returns:
            True 表示成功取消，False 表示未找到对应订阅
        """
        for event_type, subs in self._subscribers.items():
            for i, sub in enumerate(subs):
                if sub.id == subscription_id:
                    del subs[i]
                    # 清空后删除事件类型的键，避免遗留空列表
                    if not subs:
                        del self._subscribers[event_type]
                    return True
        return False

    async def publish(self, event: Event) -> None:
        """发布事件，所有订阅者并行接收

        错误隔离：单个订阅者抛异常不影响其他订阅者，
        异常会被捕获并记日志，不会冒泡到 publish 调用方。

        Args:
            event: 要发布的事件对象
        """
        # 拷贝订阅者列表快照，避免迭代期间订阅/取消订阅导致竞态
        subs = list(self._subscribers.get(event.type, []))
        if not subs:
            return

        async def _safe_call(sub: Subscription) -> None:
            """安全调用单个订阅者，捕获异常实现错误隔离"""
            try:
                if sub.is_async:
                    await sub.callback(event)
                else:
                    # 同步回调用 to_thread 包装，避免阻塞事件循环
                    # 注意：to_thread 会在线程池执行，回调内部不应访问 asyncio 同步原语
                    await asyncio.to_thread(sub.callback, event)
            except asyncio.CancelledError:
                # CancelledError 必须向上传播，不能吞掉
                raise
            except Exception as e:
                # 错误隔离：记录日志但不冒泡，其他订阅者继续执行
                logging.warning(
                    "事件订阅者异常 (event=%s, sub_id=%d): %s",
                    event.type, sub.id, e,
                    exc_info=True,
                )

        try:
            # 用 gather 并行通知所有订阅者
            await asyncio.gather(*[_safe_call(s) for s in subs])
        except asyncio.CancelledError:
            raise

    def clear(self, event_type: str = None) -> int:
        """清空订阅

        Args:
            event_type: 指定事件类型则只清空该类型，None 则清空全部

        Returns:
            被清空的订阅者数量
        """
        if event_type is None:
            # 清空全部
            count = sum(len(subs) for subs in self._subscribers.values())
            self._subscribers.clear()
            return count
        # 清空指定类型
        subs = self._subscribers.pop(event_type, [])
        return len(subs)

    def subscriber_count(self, event_type: str = None) -> int:
        """获取订阅者数量

        Args:
            event_type: 指定事件类型则返回该类型的订阅数，None 则返回全部

        Returns:
            订阅者数量
        """
        if event_type is None:
            return sum(len(subs) for subs in self._subscribers.values())
        return len(self._subscribers.get(event_type, []))


# 全局默认事件总线（单例）
_default_bus: EventBus | None = None


def get_default_bus() -> EventBus:
    """获取全局默认事件总线（懒初始化单例）

    全局单例便于不同模块（memory、skills、tools）共享同一总线，
    无需手动传递 EventBus 实例。

    Returns:
        全局默认 EventBus 实例
    """
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus


def reset_default_bus() -> None:
    """重置全局默认事件总线（主要用于测试隔离）

    清空全局单例，下次调用 get_default_bus() 会创建新实例。
    生产代码一般不需要调用此函数。
    """
    global _default_bus
    _default_bus = None
