from __future__ import annotations

from dataclasses import asdict

from scheduler.scheduler_repository import SchedulerRepository


class SchedulerService:
    def __init__(self, repository: SchedulerRepository) -> None:
        self._repository = repository

    def snapshot(self) -> dict:
        return {
            "items": [asdict(task) for task in self._repository.list_tasks()]
        }
