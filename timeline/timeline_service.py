from __future__ import annotations

from timeline.timeline_repository import TimelineRepository


class TimelineService:
    def __init__(self, repository: TimelineRepository) -> None:
        self._repository = repository

    def snapshot(self) -> dict:
        return {
            "items": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "title": event.title,
                    "details": event.details,
                    "created_at": event.created_at.isoformat(),
                }
                for event in self._repository.list_events()
            ]
        }
