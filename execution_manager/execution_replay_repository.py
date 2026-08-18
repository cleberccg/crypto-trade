from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ExecutionReplayRepository:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.results_dir = base_dir / "optimization" / "results"
        self.logs_dir = base_dir / "logs" / "execution"

    def load_state(self) -> dict[str, Any]:
        return self._read_json(self.results_dir / "execution_state.json")

    def load_jobs(self) -> list[dict[str, Any]]:
        payload = self._read_json(self.results_dir / "execution_jobs_queue.json")
        if isinstance(payload, list):
            return payload
        return []

    def load_report(self) -> dict[str, Any]:
        return self._read_json(self.results_dir / "execution_report.json")

    def load_heartbeat(self) -> dict[str, Any]:
        return self._read_json(self.results_dir / "execution_heartbeat.json")

    def list_incidents(self) -> list[dict[str, Any]]:
        incidents_dir = self.results_dir / "incidents"
        if not incidents_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for item in sorted(incidents_dir.glob("INC_*"), reverse=True):
            items.append({"id": item.name, "path": str(item)})
        return items

    def list_artifacts(self) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for file in sorted(self.results_dir.glob("*")):
            if file.is_file():
                artifacts.append({"name": file.name, "path": str(file), "size": file.stat().st_size})
        if self.logs_dir.exists():
            for file in sorted(self.logs_dir.glob("*.log")):
                artifacts.append({"name": file.name, "path": str(file), "size": file.stat().st_size})
        return artifacts

    def tail_logs(self, limit_per_file: int = 120) -> list[dict[str, Any]]:
        if not self.logs_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for file in sorted(self.logs_dir.glob("*.log")):
            lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
            items.append({"file": file.name, "tail": lines[-limit_per_file:]})
        return items

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
            return {"items": payload}
        except (json.JSONDecodeError, OSError):
            return {}
