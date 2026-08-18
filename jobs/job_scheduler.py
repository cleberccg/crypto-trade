from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class ScheduledJob:
    id: str
    job_type: str
    run_at: str
    enabled: bool = False


class JobScheduler:
    def __init__(self) -> None:
        self._items = [
            ScheduledJob(id="schedule-optimizer-daily", job_type="optimizer", run_at="02:00", enabled=False),
            ScheduledJob(id="schedule-validation-after-optimizer", job_type="validation", run_at="after-optimizer", enabled=False),
        ]

    def list(self) -> dict:
        return {
            "items": [item.__dict__ for item in self._items],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
