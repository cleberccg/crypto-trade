from __future__ import annotations

from notifications.notification_models import NotificationRecord
from notifications.notification_repository import NotificationRepository


class TelegramRepository:
    def __init__(self, base_repository: NotificationRepository) -> None:
        self._base = base_repository

    def enqueue(self, record: NotificationRecord) -> int | None:
        return self._base.create_notification(record)

    def mark_sent(self, notification_id: int, delivery_ms: float) -> None:
        self._base.mark_sent(notification_id, delivery_ms)

    def mark_failed(self, notification_id: int, error_message: str) -> None:
        self._base.mark_failed(notification_id, error_message)

    def stats(self) -> dict[str, int | str | None]:
        return self._base.stats()
