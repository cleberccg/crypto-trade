from __future__ import annotations

from dataclasses import dataclass

from core.events.event_bus import EventBus
from core.events.events import EventType, OptimizationEvent


@dataclass
class _Collector:
    items: list[OptimizationEvent]

    def handle(self, event: OptimizationEvent) -> None:
        self.items.append(event)


def test_event_bus_dispatches_to_listeners() -> None:
    collector = _Collector(items=[])
    bus = EventBus([collector], async_dispatch=False)
    event = OptimizationEvent(event_type=EventType.OPTIMIZER_STARTED, execution_id="exec-001")

    bus.publish(event)

    assert collector.items == [event]
