from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from execution_manager.execution_models import ExecutionState
from execution_manager.resource_monitor import ResourceMonitor


class HeartbeatPublisher:
    def __init__(self, state: ExecutionState, monitor: ResourceMonitor, heartbeat_file: Path, interval_seconds: int = 30) -> None:
        self.state = state
        self.monitor = monitor
        self.heartbeat_file = heartbeat_file
        self.interval_seconds = max(1, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def publish_now(self) -> None:
        now = datetime.now(tz=timezone.utc)
        self.state.last_heartbeat_at = now
        snapshot = self.state.to_dict()
        snapshot.update(self.monitor.snapshot())
        snapshot["timestamp"] = now.isoformat()
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.publish_now()
