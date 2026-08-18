from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class JobStatus(str, Enum):
    WAITING = "Waiting"
    RUNNING = "Running"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    RECOVERED = "Recovered"
    RETRIED = "Retried"


@dataclass
class ExecutionJob:
    name: str
    stage: str
    total: int = 0
    execution_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: JobStatus = JobStatus.WAITING
    processed: int = 0
    eta_seconds: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker: str | None = None
    cpu: float | None = None
    ram: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    stacktrace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "name": self.name,
            "stage": self.stage,
            "status": self.status.value,
            "processed": self.processed,
            "total": self.total,
            "eta_seconds": self.eta_seconds,
            "started_at": self.started_at.astimezone(timezone.utc).isoformat() if self.started_at else None,
            "finished_at": self.finished_at.astimezone(timezone.utc).isoformat() if self.finished_at else None,
            "worker": self.worker,
            "cpu": self.cpu,
            "ram": self.ram,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class ExecutionState:
    execution_id: str
    status: str = "CREATED"
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    finished_at: datetime | None = None
    current_job_id: str | None = None
    next_job_id: str | None = None
    progress_pct: float = 0.0
    processed_total: int = 0
    target_total: int = 0
    eta_seconds: float | None = None
    cpu: float = 0.0
    ram: float = 0.0
    last_heartbeat_at: datetime | None = None
    last_checkpoint_at: datetime | None = None
    last_log_at: datetime | None = None
    last_db_write_at: datetime | None = None
    last_progress_at: datetime | None = None
    stalled_reason: str | None = None
    recoveries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "started_at": self.started_at.astimezone(timezone.utc).isoformat(),
            "finished_at": self.finished_at.astimezone(timezone.utc).isoformat() if self.finished_at else None,
            "current_job_id": self.current_job_id,
            "next_job_id": self.next_job_id,
            "progress_pct": self.progress_pct,
            "processed_total": self.processed_total,
            "target_total": self.target_total,
            "eta_seconds": self.eta_seconds,
            "cpu": self.cpu,
            "ram": self.ram,
            "last_heartbeat_at": self.last_heartbeat_at.astimezone(timezone.utc).isoformat() if self.last_heartbeat_at else None,
            "last_checkpoint_at": self.last_checkpoint_at.astimezone(timezone.utc).isoformat() if self.last_checkpoint_at else None,
            "last_log_at": self.last_log_at.astimezone(timezone.utc).isoformat() if self.last_log_at else None,
            "last_db_write_at": self.last_db_write_at.astimezone(timezone.utc).isoformat() if self.last_db_write_at else None,
            "last_progress_at": self.last_progress_at.astimezone(timezone.utc).isoformat() if self.last_progress_at else None,
            "stalled_reason": self.stalled_reason,
            "recoveries": self.recoveries,
        }
