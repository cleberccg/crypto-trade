from __future__ import annotations

from execution_manager.execution_models import ExecutionState
from night_runner import NightRunner


class RecoveryManager:
    def __init__(self, state: ExecutionState) -> None:
        self.state = state

    def validate_checkpoint(self, execution_id: str, symbol: str, timeframe: str) -> tuple[bool, str]:
        runner = NightRunner(dry_run=True)
        return runner._validate_resume_checkpoint(execution_id, symbol, timeframe)

    def decide_resume(self, execution_id: str, symbol: str, timeframe: str) -> bool:
        valid, _reason = self.validate_checkpoint(execution_id, symbol, timeframe)
        return valid
