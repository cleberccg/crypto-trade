from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

JobStatus = Literal["queued", "running", "paused", "completed", "failed"]
JobType = Literal["download", "optimizer", "validation", "backup", "research", "paper_trading", "live_trading", "dashboard"]


@dataclass(slots=True)
class JobRecord:
    id: str
    name: str
    job_type: JobType
    status: JobStatus
    progress_pct: float
    worker: str | None = None
    cpu_pct: float | None = None
    ram_pct: float | None = None
    eta_seconds: int | None = None
    logs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
