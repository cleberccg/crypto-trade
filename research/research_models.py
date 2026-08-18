from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ResearchInsight:
    id: str
    title: str
    summary: str
    category: str
