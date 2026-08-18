from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from uuid import uuid4

from config.settings import settings
from database.connection import get_session
from notifications.notification_models import NotificationRecord
from notifications.notification_repository import NotificationRepository
from notifications.telegram_commands import TelegramCommands
from notifications.telegram_repository import TelegramRepository
from notifications.telegram_router import TelegramRouter
from notifications.telegram_scheduler import TelegramScheduler
from notifications.telegram_service import TelegramService
from notifications.telegram_templates import event_message
from utils.logger import get_logger

logger = get_logger(__name__)


class NotificationService:
    def __init__(self, repository: NotificationRepository) -> None:
        self._repository = repository
        self._enabled = bool(settings.telegram.enabled)
        self._telegram = TelegramService(settings.telegram.bot_token)
        self._queue: queue.Queue[tuple[str, str, str, str | None, str]] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._scheduler: TelegramScheduler | None = None
        authorized = {cid for cid in [settings.telegram.chat_id, settings.telegram.admin_chat_id] if cid}
        self._router = TelegramRouter(TelegramCommands(authorized_chat_ids=authorized))

    def start(self) -> None:
        if not self._enabled:
            return
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._run_worker, daemon=True)
            self._worker.start()

        if settings.telegram.send_progress:
            if self._scheduler is None:
                self._scheduler = TelegramScheduler(
                    interval_minutes=settings.telegram.progress_interval_minutes,
                    sender=self.publish,
                )
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._worker is not None:
            self._worker.join(timeout=2)

    def publish(self, notification_type: str, title: str, message: str, execution_id: str | None = None) -> None:
        if not self._enabled:
            return
        destination = settings.telegram.chat_id
        if notification_type in {
            "critical_failure",
            "unhandled_error",
            "worker_dead",
            "optimizer_stopped",
            "heartbeat_lost",
            "database_unavailable",
            "download_failed",
            "critical_exception",
            "recovery_started",
            "recovery_finished",
        }:
            destination = settings.telegram.admin_chat_id or destination

        self._queue.put((notification_type, title, message, execution_id, destination))

    def publish_event(self, event_type: str, payload: dict) -> None:
        # Channel toggles
        if event_type.endswith("failed") and not settings.telegram.send_errors:
            return
        if event_type.endswith("finished") and not settings.telegram.send_success:
            return

        title, message = event_message(event_type, payload)
        execution_id = payload.get("execution_id") if isinstance(payload, dict) else None
        self.publish(event_type, title, message, execution_id=execution_id)

    def handle_telegram_command(self, chat_id: str, command: str) -> str:
        response = self._router.dispatch_command(command, chat_id)
        return response.text

    def snapshot(self) -> dict:
        return {
            "items": [
                {
                    "id": item.id,
                    "channel": item.channel,
                    "notification_type": item.notification_type,
                    "title": item.title,
                    "message": item.message,
                    "execution_id": item.execution_id,
                    "destination": item.destination,
                    "delivery_ms": item.delivery_ms,
                    "error_message": item.error_message,
                    "status": item.status,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self._repository.list_notifications()
            ]
        }

    def telemetry(self) -> dict:
        stats = self._repository.stats()
        return {
            "enabled": self._enabled,
            "status": "online" if self._enabled else "offline",
            "last_sent_at": stats.get("last_sent_at"),
            "messages_sent": stats.get("sent", 0),
            "messages_error": stats.get("errors", 0),
            "queue_size": self._queue.qsize(),
        }

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                notification_type, title, message, execution_id, destination = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            created_at = datetime.now(timezone.utc)
            with get_session() as session:
                repo = TelegramRepository(NotificationRepository(session=session))
                record = NotificationRecord(
                    id=str(uuid4()),
                    channel="telegram",
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    execution_id=execution_id,
                    destination=destination,
                    status="queued",
                    created_at=created_at,
                )
                row_id = repo.enqueue(record)

                result = self._telegram.send_message(chat_id=destination, text=f"{title}\n{message}")
                if row_id is not None:
                    if result.ok:
                        repo.mark_sent(notification_id=row_id, delivery_ms=result.delivery_ms)
                    else:
                        repo.mark_failed(notification_id=row_id, error_message=result.error or "telegram_send_failed")

            self._queue.task_done()


_GLOBAL_SERVICE: NotificationService | None = None


def get_notification_service() -> NotificationService:
    global _GLOBAL_SERVICE
    if _GLOBAL_SERVICE is None:
        _GLOBAL_SERVICE = NotificationService(NotificationRepository())
    return _GLOBAL_SERVICE

    def snapshot(self) -> dict:
        return {
            "items": [
                {
                    "id": item.id,
                    "channel": item.channel,
                    "title": item.title,
                    "message": item.message,
                    "status": item.status,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self._repository.list_notifications()
            ]
        }
