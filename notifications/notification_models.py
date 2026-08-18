from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class NotificationRecord:
    id: str
    channel: str
    title: str
    message: str
    notification_type: str = "generic"
    execution_id: str | None = None
    destination: str = ""
    delivery_ms: float | None = None
    error_message: str | None = None
    status: str = "stored"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
