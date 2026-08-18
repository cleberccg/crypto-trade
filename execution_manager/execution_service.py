from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from database.history_service import HistoryPersistenceService
from execution_manager.execution_models import ExecutionState
from execution_manager.execution_repository import ExecutionRepository


class ExecutionService:
    def __init__(self, state: ExecutionState, repository: ExecutionRepository) -> None:
        self.state = state
        self.repository = repository

    @classmethod
    def create_default(cls, execution_id: str, base_dir: Path) -> "ExecutionService":
        state = ExecutionState(execution_id=execution_id, status="Idle", started_at=datetime.now(tz=timezone.utc))
        repo = ExecutionRepository(base_dir / "optimization" / "results" / "execution_state.json")
        return cls(state=state, repository=repo)

    def persist(self) -> None:
        self.repository.save(self.state.to_dict())

    @staticmethod
    def new_execution_id() -> str:
        return HistoryPersistenceService.new_execution_id()
