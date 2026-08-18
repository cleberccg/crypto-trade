"""Validation result models for optimization statistical analysis."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationEntry:
    """Stores the train/validation metrics and validation decision for one configuration."""

    rank: int
    parameters: dict[str, Any]
    train_metrics: dict[str, float | int]
    validation_metrics: dict[str, float | int]
    passed: bool
    discard_reasons: list[str]
    overfitting_risk: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "parameters": self.parameters,
            "train_metrics": self.train_metrics,
            "validation_metrics": self.validation_metrics,
            "passed": self.passed,
            "discard_reasons": self.discard_reasons,
            "overfitting_risk": self.overfitting_risk,
        }
