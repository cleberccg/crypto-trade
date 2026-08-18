from __future__ import annotations

from collections.abc import Iterable

from scheduler.scheduler_models import SchedulerTask


class SchedulerRepository:
    def __init__(self, tasks: Iterable[SchedulerTask] | None = None) -> None:
        self._tasks = list(tasks or [])

    def list_tasks(self) -> list[SchedulerTask]:
        return self._tasks
