from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Iterable
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database.history_models import NotificationHistory
from notifications.notification_models import NotificationRecord


class NotificationRepository:
    def __init__(self, session: Session | None = None, notifications: Iterable[NotificationRecord] | None = None) -> None:
        self._session = session
        self._notifications = list(notifications or [])

    def list_notifications(self) -> list[NotificationRecord]:
        if self._session is not None:
            rows = (
                self._session.query(NotificationHistory)
                .order_by(desc(NotificationHistory.created_at), desc(NotificationHistory.id))
                .limit(500)
                .all()
            )
            return [
                NotificationRecord(
                    id=str(row.id),
                    channel=row.channel,
                    notification_type=row.notification_type,
                    title=row.title,
                    message=row.message,
                    execution_id=row.execution_id,
                    destination=row.destination,
                    delivery_ms=row.delivery_ms,
                    error_message=row.error_message,
                    status=row.status,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        return sorted(self._notifications, key=lambda item: item.created_at, reverse=True)

    def create_notification(self, record: NotificationRecord) -> int | None:
        if self._session is None:
            self._notifications.append(record)
            return None

        row = NotificationHistory(
            notification_type=record.notification_type,
            title=record.title,
            message=record.message,
            execution_id=record.execution_id,
            status=record.status,
            destination=record.destination,
            channel=record.channel,
            delivery_ms=record.delivery_ms,
            error_message=record.error_message,
            created_at=record.created_at,
        )
        self._session.add(row)
        self._session.flush()
        return int(row.id)

    def mark_sent(self, notification_id: int, delivery_ms: float) -> None:
        if self._session is None:
            return
        row = self._session.query(NotificationHistory).filter_by(id=notification_id).first()
        if row is None:
            return
        row.status = "sent"
        row.delivery_ms = delivery_ms

    def mark_failed(self, notification_id: int, error_message: str) -> None:
        if self._session is None:
            return
        row = self._session.query(NotificationHistory).filter_by(id=notification_id).first()
        if row is None:
            return
        row.status = "error"
        row.error_message = error_message

    def stats(self) -> dict[str, int | str | None]:
        if self._session is None:
            queued = len([n for n in self._notifications if n.status == "queued"])
            sent = len([n for n in self._notifications if n.status == "sent"])
            errors = len([n for n in self._notifications if n.status == "error"])
            last = max((n.created_at for n in self._notifications), default=None)
            return {
                "queued": queued,
                "sent": sent,
                "errors": errors,
                "last_sent_at": last.isoformat() if last else None,
            }

        queued = self._session.query(NotificationHistory).filter_by(status="queued").count()
        sent = self._session.query(NotificationHistory).filter_by(status="sent").count()
        errors = self._session.query(NotificationHistory).filter_by(status="error").count()
        last = (
            self._session.query(NotificationHistory)
            .order_by(desc(NotificationHistory.created_at), desc(NotificationHistory.id))
            .first()
        )
        return {
            "queued": int(queued),
            "sent": int(sent),
            "errors": int(errors),
            "last_sent_at": last.created_at.astimezone(timezone.utc).isoformat() if last else None,
        }
