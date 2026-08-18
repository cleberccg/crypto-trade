from __future__ import annotations

from datetime import datetime, timezone

from notifications.notification_models import NotificationRecord
from notifications.notification_repository import NotificationRepository
from notifications.notification_service import NotificationService
from notifications.telegram_commands import TelegramCommands
from notifications.telegram_service import TelegramService
from notifications.telegram_templates import event_message, periodic_progress


def test_templates_render_event_and_progress() -> None:
    title, message = event_message("optimizer_started", {"execution_id": "exec-1", "symbol": "BTC/USDT", "timeframe": "5m"})
    assert "optimizer_started" in title
    assert "exec-1" in message

    p_title, p_message = periodic_progress({"execution_id": "exec-1", "status": "running", "progress_pct": 12.0})
    assert "progress_report" in p_title
    assert "exec-1" in p_message


def test_notification_repository_in_memory_stats() -> None:
    repo = NotificationRepository()
    repo.create_notification(
        NotificationRecord(
            id="1",
            channel="telegram",
            notification_type="checkpoint",
            title="[checkpoint]",
            message="saved",
            execution_id="exec-1",
            destination="chat",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
    )
    stats = repo.stats()
    assert int(stats["queued"]) == 1


def test_commands_authorization_and_help() -> None:
    commands = TelegramCommands(authorized_chat_ids={"admin-chat"})
    help_resp = commands.handle("/help", "guest")
    assert help_resp.ok
    assert "/status" in help_resp.text

    denied = commands.handle("/status", "guest")
    assert not denied.ok


def test_telegram_service_missing_token() -> None:
    svc = TelegramService(bot_token="")
    result = svc.send_message(chat_id="chat", text="hello")
    assert not result.ok
    assert result.error == "missing_bot_token"


def test_notification_service_snapshot_and_telemetry() -> None:
    svc = NotificationService(NotificationRepository())
    snap = svc.snapshot()
    assert "items" in snap
    telem = svc.telemetry()
    assert "status" in telem
