from __future__ import annotations

from dataclasses import asdict

from research.research_repository import ResearchRepository


class ResearchService:
    def __init__(self, repository: ResearchRepository) -> None:
        self._repository = repository

    def snapshot(self) -> dict:
        return {"items": [asdict(item) for item in self._repository.list_insights()]}
