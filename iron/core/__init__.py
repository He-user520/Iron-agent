"""Iron 核心基础设施

包含跨模块共享的基础设施组件：
- pubsub: PubSub 事件总线（解耦事件生产者和消费者）
"""
from iron.core.pubsub import EventBus, Event, Subscription, get_default_bus, reset_default_bus

__all__ = ["EventBus", "Event", "Subscription", "get_default_bus", "reset_default_bus"]
