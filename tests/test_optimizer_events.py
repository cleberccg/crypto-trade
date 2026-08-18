"""Tests for optimizer event publishing and listener fanout."""
from __future__ import annotations

from core.events import EventBus, EventType, OptimizationEvent
from core.events.interfaces import EventListener


class _CaptureListener(EventListener):
    def __init__(self) -> None:
        self.events: list[OptimizationEvent] = []

    def handle(self, event: OptimizationEvent) -> None:
        self.events.append(event)


def test_event_bus_delivers_to_multiple_listeners() -> None:
    a = _CaptureListener()
    b = _CaptureListener()
    bus = EventBus([a, b])

    event = OptimizationEvent(event_type=EventType.OPTIMIZER_STARTED, execution_id="exec-1", payload={"x": 1})
    bus.publish(event)

    assert len(a.events) == 1
    assert len(b.events) == 1
    assert a.events[0].event_type == EventType.OPTIMIZER_STARTED


def test_event_bus_subscribe_runtime() -> None:
    bus = EventBus([])
    listener = _CaptureListener()
    bus.subscribe(listener)
    bus.publish(OptimizationEvent(event_type=EventType.CHECKPOINT, execution_id="exec-2", payload={"p": 10}))

    assert len(listener.events) == 1
    assert listener.events[0].payload["p"] == 10
