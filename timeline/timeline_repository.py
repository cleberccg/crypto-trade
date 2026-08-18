from __future__ import annotations

from collections.abc import Iterable

from timeline.timeline_models import TimelineEvent


class TimelineRepository:
    def __init__(self, events: Iterable[TimelineEvent] | None = None) -> None:
        self._events = list(events or [])

    def list_events(self) -> list[TimelineEvent]:
        return sorted(self._events, key=lambda event: event.created_at, reverse=True)
