from __future__ import annotations

from datetime import datetime, timezone

from execution_manager.execution_models import ExecutionState


class ProgressTracker:
    def __init__(self, state: ExecutionState) -> None:
        self.state = state

    def update(self, processed: int, target: int) -> None:
        self.state.processed_total = max(0, processed)
        self.state.target_total = max(0, target)
        if target > 0:
            self.state.progress_pct = round((processed / target) * 100.0, 2)
        else:
            self.state.progress_pct = 0.0
        now = datetime.now(tz=timezone.utc)
        self.state.last_log_at = now
        self.state.last_progress_at = now

    def set_checkpoint(self) -> None:
        self.state.last_checkpoint_at = datetime.now(tz=timezone.utc)

    def set_db_write(self) -> None:
        self.state.last_db_write_at = datetime.now(tz=timezone.utc)
