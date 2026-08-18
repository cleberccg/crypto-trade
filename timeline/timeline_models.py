from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class TimelineEvent:
    id: str
    event_type: str
    title: str
    details: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
