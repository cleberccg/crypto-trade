from __future__ import annotations

from core.events.events import EventType, OptimizationEvent
from core.events.interfaces import EventListener
from notifications.notification_service import get_notification_service


class TelegramEventListener(EventListener):
    def handle(self, event: OptimizationEvent) -> None:
        service = get_notification_service()
        payload = dict(event.payload)
        payload["execution_id"] = event.execution_id
        service.publish_event(event.event_type.value, payload)


def make_telegram_listener() -> TelegramEventListener:
    return TelegramEventListener()


CRITICAL_EVENT_MAP = {
    EventType.OPTIMIZER_STARTED: "optimizer_started",
    EventType.OPTIMIZER_FINISHED: "optimizer_finished",
    EventType.CHECKPOINT: "checkpoint",
}
