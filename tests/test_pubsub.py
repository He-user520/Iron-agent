"""PubSub 事件总线单元测试

覆盖 iron.core.pubsub 的核心功能：
- 基本订阅/发布
- 多订阅者广播
- 取消订阅
- async/sync 回调混合
- 错误隔离（单个订阅者异常不影响其他）
- 清空订阅
- 订阅者计数
- 全局默认总线单例
- 并发发布安全

运行方式: pytest tests/test_pubsub.py -v
"""
import asyncio

import pytest

from iron.core.pubsub import (
    EventBus,
    Event,
    Subscription,
    get_default_bus,
    reset_default_bus,
)


class TestSubscribePublish:
    """基本订阅和发布"""

    async def test_subscribe_publish(self):
        """订阅者应收到发布的事件"""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe("test.event", handler)
        await bus.publish(Event("test.event", {"key": "value"}))

        assert len(received) == 1
        assert received[0].type == "test.event"
        assert received[0].data == {"key": "value"}
        # timestamp 应被自动填充
        assert received[0].timestamp is not None

    async def test_publish_no_subscribers(self):
        """无订阅者时 publish 不应报错"""
        bus = EventBus()
        # 不应有异常抛出
        await bus.publish(Event("orphan.event", {"data": 1}))


class TestMultipleSubscribers:
    """多个订阅者都收到"""

    async def test_multiple_subscribers(self):
        """同一事件类型的多个订阅者应并行全部收到"""
        bus = EventBus()
        results_a = []
        results_b = []
        results_c = []

        async def handler_a(event: Event):
            results_a.append(event.data["n"])

        async def handler_b(event: Event):
            results_b.append(event.data["n"])

        async def handler_c(event: Event):
            results_c.append(event.data["n"])

        bus.subscribe("num.event", handler_a)
        bus.subscribe("num.event", handler_b)
        bus.subscribe("num.event", handler_c)

        await bus.publish(Event("num.event", {"n": 1}))
        await bus.publish(Event("num.event", {"n": 2}))

        assert results_a == [1, 2]
        assert results_b == [1, 2]
        assert results_c == [1, 2]

    async def test_different_event_types_isolated(self):
        """不同事件类型的订阅者互不干扰"""
        bus = EventBus()
        a_events = []
        b_events = []

        async def handler_a(event: Event):
            a_events.append(event.type)

        async def handler_b(event: Event):
            b_events.append(event.type)

        bus.subscribe("type.a", handler_a)
        bus.subscribe("type.b", handler_b)

        await bus.publish(Event("type.a", {}))
        await bus.publish(Event("type.b", {}))
        await bus.publish(Event("type.a", {}))

        assert a_events == ["type.a", "type.a"]
        assert b_events == ["type.b"]


class TestUnsubscribe:
    """取消订阅后不再收到"""

    async def test_unsubscribe(self):
        """取消订阅后不再收到事件"""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event.data["v"])

        sub_id = bus.subscribe("cancel.event", handler)
        await bus.publish(Event("cancel.event", {"v": 1}))
        assert received == [1]

        # 取消订阅
        ok = bus.unsubscribe(sub_id)
        assert ok is True

        await bus.publish(Event("cancel.event", {"v": 2}))
        # 应不再收到
        assert received == [1]

    def test_unsubscribe_invalid_id(self):
        """取消不存在的订阅 ID 应返回 False"""
        bus = EventBus()
        assert bus.unsubscribe(99999) is False
        assert bus.unsubscribe(0) is False

    async def test_unsubscribe_one_of_many(self):
        """取消多个订阅者中的一个，其他仍正常"""
        bus = EventBus()
        a, b = [], []

        async def ha(e: Event):
            a.append(e.data["v"])

        async def hb(e: Event):
            b.append(e.data["v"])

        sid_a = bus.subscribe("evt", ha)
        bus.subscribe("evt", hb)

        await bus.publish(Event("evt", {"v": 1}))
        assert a == [1] and b == [1]

        bus.unsubscribe(sid_a)
        await bus.publish(Event("evt", {"v": 2}))
        # a 已取消，b 仍收到
        assert a == [1]
        assert b == [1, 2]


class TestAsyncCallback:
    """异步回调正常执行"""

    async def test_async_callback(self):
        """async 回调应被正确 await"""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            # 模拟异步操作
            await asyncio.sleep(0.001)
            received.append(event.data["x"])

        bus.subscribe("async.event", handler)
        await bus.publish(Event("async.event", {"x": 42}))

        assert received == [42]

    async def test_async_callback_awaited_in_order(self):
        """async 回调内部的 await 完成后才返回 publish"""
        bus = EventBus()
        order = []

        async def handler(event: Event):
            await asyncio.sleep(0.01)
            order.append("handler_done")

        bus.subscribe("ordered.event", handler)
        await bus.publish(Event("ordered.event", {}))
        # publish 返回时 handler 应已完成
        order.append("publish_done")
        assert order == ["handler_done", "publish_done"]


class TestSyncCallback:
    """同步回调正常执行"""

    async def test_sync_callback(self):
        """sync 回调应被 to_thread 包装执行"""
        bus = EventBus()
        received = []

        def handler(event: Event):
            # 同步函数，不应是 async
            received.append(event.data["v"])

        bus.subscribe_sync("sync.event", handler)
        await bus.publish(Event("sync.event", {"v": 7}))

        assert received == [7]

    async def test_subscribe_auto_detects_sync(self):
        """subscribe 也应自动检测 sync 回调并正确处理"""
        bus = EventBus()
        received = []

        def sync_handler(event: Event):
            received.append(event.data["v"])

        # 用 subscribe（非 subscribe_sync）订阅 sync 回调
        bus.subscribe("auto.event", sync_handler)
        await bus.publish(Event("auto.event", {"v": 99}))

        assert received == [99]

    async def test_mixed_async_sync_callbacks(self):
        """同一事件混合 async 和 sync 回调都应正常执行"""
        bus = EventBus()
        async_results = []
        sync_results = []

        async def async_handler(event: Event):
            await asyncio.sleep(0.001)
            async_results.append(event.data["v"])

        def sync_handler(event: Event):
            sync_results.append(event.data["v"])

        bus.subscribe("mixed.event", async_handler)
        bus.subscribe_sync("mixed.event", sync_handler)

        await bus.publish(Event("mixed.event", {"v": 5}))

        assert async_results == [5]
        assert sync_results == [5]


class TestErrorIsolation:
    """单个回调异常不影响其他"""

    async def test_error_isolation(self):
        """一个订阅者抛异常，其他订阅者仍应收到事件"""
        bus = EventBus()
        ok_results = []

        async def bad_handler(event: Event):
            raise RuntimeError("故意抛异常")

        async def good_handler(event: Event):
            ok_results.append(event.data["v"])

        bus.subscribe("isolated.event", bad_handler)
        bus.subscribe("isolated.event", good_handler)

        # publish 不应抛异常
        await bus.publish(Event("isolated.event", {"v": 1}))

        # good_handler 应正常收到
        assert ok_results == [1]

    async def test_sync_error_isolation(self):
        """sync 回调抛异常也应被隔离"""
        bus = EventBus()
        ok_results = []

        def bad_sync(event: Event):
            raise ValueError("sync 异常")

        async def good_async(event: Event):
            ok_results.append(event.data["v"])

        bus.subscribe_sync("iso.sync", bad_sync)
        bus.subscribe("iso.sync", good_async)

        await bus.publish(Event("iso.sync", {"v": 2}))
        assert ok_results == [2]

    async def test_error_in_first_handler_does_not_block_others(self):
        """第一个订阅者抛异常不应阻塞后续订阅者"""
        bus = EventBus()
        results = []

        async def first(event: Event):
            raise RuntimeError("第一个挂了")

        async def second(event: Event):
            results.append("second_ok")

        async def third(event: Event):
            results.append("third_ok")

        bus.subscribe("chain.event", first)
        bus.subscribe("chain.event", second)
        bus.subscribe("chain.event", third)

        await bus.publish(Event("chain.event", {}))
        assert "second_ok" in results
        assert "third_ok" in results


class TestClear:
    """清空订阅"""

    async def test_clear_specific_event_type(self):
        """clear 指定事件类型只清空该类型"""
        bus = EventBus()
        a_received = []
        b_received = []

        async def ha(e: Event):
            a_received.append(1)

        async def hb(e: Event):
            b_received.append(1)

        bus.subscribe("type.a", ha)
        bus.subscribe("type.b", hb)

        count = bus.clear("type.a")
        assert count == 1

        await bus.publish(Event("type.a", {}))
        await bus.publish(Event("type.b", {}))

        assert a_received == []
        assert b_received == [1]

    async def test_clear_all(self):
        """clear() 不传参数清空全部订阅"""
        bus = EventBus()

        async def h(e: Event):
            pass

        bus.subscribe("a", h)
        bus.subscribe("b", h)
        bus.subscribe("c", h)

        count = bus.clear()
        assert count == 3
        assert bus.subscriber_count() == 0

    def test_clear_nonexistent_event_type(self):
        """clear 不存在的事件类型应返回 0"""
        bus = EventBus()
        assert bus.clear("nonexistent") == 0


class TestSubscriberCount:
    """订阅者计数"""

    def test_subscriber_count(self):
        """计数应准确反映订阅者数量"""
        bus = EventBus()

        async def h(e: Event):
            pass

        assert bus.subscriber_count() == 0
        assert bus.subscriber_count("evt") == 0

        s1 = bus.subscribe("evt", h)
        assert bus.subscriber_count("evt") == 1
        assert bus.subscriber_count() == 1

        s2 = bus.subscribe("evt", h)
        assert bus.subscriber_count("evt") == 2
        assert bus.subscriber_count() == 2

        bus.subscribe("other", h)
        assert bus.subscriber_count("evt") == 2
        assert bus.subscriber_count("other") == 1
        assert bus.subscriber_count() == 3

        bus.unsubscribe(s1)
        assert bus.subscriber_count("evt") == 1
        assert bus.subscriber_count() == 2

        bus.unsubscribe(s2)
        assert bus.subscriber_count("evt") == 0
        # type.a 已被 unsubscribe 清空时不应再计入
        assert bus.subscriber_count() == 1


class TestDefaultBus:
    """全局默认总线单例"""

    def test_default_bus_singleton(self):
        """get_default_bus 应返回同一实例"""
        reset_default_bus()
        bus1 = get_default_bus()
        bus2 = get_default_bus()
        assert bus1 is bus2

    def test_default_bus_is_event_bus(self):
        """默认总线应是 EventBus 实例"""
        reset_default_bus()
        bus = get_default_bus()
        assert isinstance(bus, EventBus)

    def test_reset_default_bus(self):
        """reset_default_bus 后应创建新实例"""
        reset_default_bus()
        bus1 = get_default_bus()
        reset_default_bus()
        bus2 = get_default_bus()
        # 重置后是新实例
        assert bus1 is not bus2

    async def test_default_bus_functional(self):
        """默认总线应能正常订阅和发布"""
        reset_default_bus()
        bus = get_default_bus()
        received = []

        async def handler(event: Event):
            received.append(event.data["v"])

        sub_id = bus.subscribe("default.test", handler)
        try:
            await bus.publish(Event("default.test", {"v": 100}))
            assert received == [100]
        finally:
            bus.unsubscribe(sub_id)
            reset_default_bus()


class TestConcurrentPublish:
    """并发发布安全"""

    async def test_concurrent_publish(self):
        """多个 publish 并发执行应全部正确送达"""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event.data["i"])

        bus.subscribe("concurrent.event", handler)

        # 并发发布 50 个事件
        await asyncio.gather(*[
            bus.publish(Event("concurrent.event", {"i": i}))
            for i in range(50)
        ])

        # 应收到全部 50 个（顺序可能交错，但数量必须对）
        assert len(received) == 50
        assert sorted(received) == list(range(50))

    async def test_concurrent_publish_multiple_subscribers(self):
        """多个订阅者 + 多个并发 publish 应全部正确"""
        bus = EventBus()
        a_results = []
        b_results = []

        async def ha(e: Event):
            a_results.append(e.data["i"])

        async def hb(e: Event):
            b_results.append(e.data["i"])

        bus.subscribe("multi.concurrent", ha)
        bus.subscribe("multi.concurrent", hb)

        await asyncio.gather(*[
            bus.publish(Event("multi.concurrent", {"i": i}))
            for i in range(20)
        ])

        assert len(a_results) == 20
        assert len(b_results) == 20
        assert sorted(a_results) == list(range(20))
        assert sorted(b_results) == list(range(20))


class TestSubscriptionDataclass:
    """Subscription dataclass 基本行为"""

    def test_subscription_fields(self):
        """Subscription 应正确存储字段"""
        async def h(e: Event):
            pass

        sub = Subscription(id=1, event_type="test", callback=h, is_async=True)
        assert sub.id == 1
        assert sub.event_type == "test"
        assert sub.callback is h
        assert sub.is_async is True

    def test_event_default_timestamp(self):
        """Event 不传 timestamp 应自动填充当前时间"""
        from datetime import datetime
        before = datetime.now()
        event = Event("test", {"k": "v"})
        after = datetime.now()
        assert before <= event.timestamp <= after

    def test_event_explicit_timestamp(self):
        """Event 显式传入 timestamp 应被保留"""
        from datetime import datetime
        ts = datetime(2020, 1, 1)
        event = Event("test", {}, timestamp=ts)
        assert event.timestamp == ts


class TestEngineIntegration:
    """engine.py 集成测试 — 验证事件总线在 process() 中正常工作"""

    async def test_engine_emits_events_to_bus(self, tmp_path):
        """AgentEngine.process() 应通过事件总线发布事件"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        # 用独立 EventBus 实例，避免污染全局
        bus = EventBus()
        received_types = []

        async def capture(event: Event):
            received_types.append(event.type)

        # 订阅几种关键事件类型
        for et in ["thinking", "phase", "chat_response", "chat_chunk", "step_done",
                   "summary", "error"]:
            bus.subscribe(et, capture)

        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path),
                mcu="stm32f407",
                build_system="platformio",
            ),
            mcp={},
        )
        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
            event_bus=bus,
        )

        # 消费所有事件
        async for _event in engine.process("hello"):
            pass

        # 应至少收到 thinking 和 phase 事件
        assert "thinking" in received_types
        assert "phase" in received_types

    async def test_engine_default_bus_when_not_injected(self, tmp_path):
        """未注入 event_bus 时应使用全局默认总线"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        reset_default_bus()
        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path),
                mcu="stm32f407",
                build_system="platformio",
            ),
            mcp={},
        )
        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        # 应使用全局默认总线
        assert engine._event_bus is get_default_bus()
        reset_default_bus()

    async def test_engine_bus_exception_does_not_block(self, tmp_path):
        """事件总线异常不应阻塞 Agent 主流程"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        # 创建一个会抛异常的 EventBus 子类
        class BadBus(EventBus):
            async def publish(self, event: Event) -> None:
                raise RuntimeError("总线故障")

        bus = BadBus()
        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path),
                mcu="stm32f407",
                build_system="platformio",
            ),
            mcp={},
        )
        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
            event_bus=bus,
        )

        # process 应正常完成，不抛异常
        event_count = 0
        async for _event in engine.process("hello"):
            event_count += 1
        # 应该有事件被 yield（_emit_event 兜底捕获总线异常后仍返回 AgentEvent）
        assert event_count > 0
