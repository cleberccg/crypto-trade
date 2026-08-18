from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SchedulerTask:
    id: str
    name: str
    schedule: str
    enabled: bool
