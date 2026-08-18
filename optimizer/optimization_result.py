"""Optimization result model and serialization helpers."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class OptimizationResult:
    """Single strategy configuration evaluation."""

    rank: int | None
    parameters: dict[str, Any]
    metrics: dict[str, float | int]
    combinations_tested: int
    runtime_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data
