from __future__ import annotations

from collections.abc import Iterable

from research.research_models import ResearchInsight


class ResearchRepository:
    def __init__(self, insights: Iterable[ResearchInsight] | None = None) -> None:
        self._insights = list(insights or [])

    def list_insights(self) -> list[ResearchInsight]:
        return self._insights
