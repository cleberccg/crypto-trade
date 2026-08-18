"""Event contracts for optimizer and backtest orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    OPTIMIZER_STARTED = "optimizer_started"
    COMBINATION_STARTED = "combination_started"
    COMBINATION_FINISHED = "combination_finished"
    COMBINATION_SAVED = "combination_saved"
    CHECKPOINT = "checkpoint"
    OPTIMIZER_FINISHED = "optimizer_finished"
    BACKTEST_STARTED = "backtest_started"
    BACKTEST_FINISHED = "backtest_finished"


@dataclass(frozen=True)
class OptimizationEvent:
    event_type: EventType
    execution_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
