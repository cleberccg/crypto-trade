from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class ReadinessCheck:
    id: str
    component: str
    status: str
    details: str
    activation_required: bool
    checked_at: datetime


@dataclass(slots=True)
class ActivationStep:
    id: str
    name: str
    description: str
    enabled: bool
    dry_run_only: bool = True
    updated_at: datetime = datetime.now(timezone.utc)
