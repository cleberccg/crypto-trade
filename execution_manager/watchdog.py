from __future__ import annotations

from datetime import datetime, timezone

from execution_manager.execution_models import ExecutionState


class Watchdog:
    def __init__(self, state: ExecutionState, warning_seconds: int = 600, critical_seconds: int = 900, no_progress_seconds: int = 600) -> None:
        self.state = state
        self.warning_seconds = warning_seconds
        self.critical_seconds = critical_seconds
        self.no_progress_seconds = no_progress_seconds

    def evaluate(self) -> tuple[str, float, str | None]:
        now = datetime.now(tz=timezone.utc)
        refs = [
            self.state.last_heartbeat_at,
            self.state.last_checkpoint_at,
            self.state.last_log_at,
            self.state.last_db_write_at,
        ]
        valid = [r for r in refs if r is not None]
        if not valid:
            stalled = (now - self.state.started_at).total_seconds()
        else:
            stalled = (now - max(valid)).total_seconds()

        no_progress = None
        if self.state.last_progress_at is not None:
            no_progress = (now - self.state.last_progress_at).total_seconds()

        if no_progress is not None and no_progress >= self.no_progress_seconds:
            return "critical", no_progress, "no_progress"
        if stalled >= self.critical_seconds:
            return "critical", stalled, "heartbeat_stall"
        if stalled >= self.warning_seconds:
            return "warning", stalled, None
        return "ok", stalled, None
