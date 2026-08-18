from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from execution_manager.execution_replay_repository import ExecutionReplayRepository
from execution_manager.metrics_repository import ExecutionMetricsRepository


class ExecutionReplayService:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.repo = ExecutionReplayRepository(base_dir)
        self.metrics_repo = ExecutionMetricsRepository()

    def execution(self, execution_id: str) -> dict[str, Any]:
        state = self.repo.load_state()
        jobs = self.repo.load_jobs()
        report = self.repo.load_report()
        heartbeat = self.repo.load_heartbeat()

        if state and state.get("execution_id") and state.get("execution_id") != execution_id:
            return {
                "execution_id": execution_id,
                "status": "not_found",
                "message": "Execution id is not available in local replay artifacts",
            }

        metrics = self.metrics_repo.get(execution_id)
        timeline = _build_timeline_from_jobs(jobs)
        return {
            "execution_id": execution_id,
            "state": state,
            "report": report,
            "heartbeat": heartbeat,
            "metrics": metrics,
            "timeline": timeline,
            "jobs": jobs,
            "incidents": self.repo.list_incidents(),
        }

    def timeline(self, execution_id: str) -> list[dict[str, Any]]:
        payload = self.execution(execution_id)
        return payload.get("timeline", [])

    def jobs(self, execution_id: str) -> list[dict[str, Any]]:
        payload = self.execution(execution_id)
        return payload.get("jobs", [])

    def metrics(self, execution_id: str) -> dict[str, Any]:
        payload = self.metrics_repo.get(execution_id)
        if payload is None:
            return {}
        return payload

    def artifacts(self, execution_id: str) -> dict[str, Any]:
        payload = self.execution(execution_id)
        if payload.get("status") == "not_found":
            return {"items": []}
        return {
            "items": self.repo.list_artifacts(),
            "logs": self.repo.tail_logs(),
        }


def _build_timeline_from_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for item in jobs:
        started = item.get("started_at")
        finished = item.get("finished_at")
        if started:
            timeline.append(
                {
                    "event_type": "job_started",
                    "title": str(item.get("name") or item.get("stage") or "job"),
                    "details": f"status={item.get('status')}",
                    "created_at": started,
                }
            )
        if finished:
            timeline.append(
                {
                    "event_type": "job_finished",
                    "title": str(item.get("name") or item.get("stage") or "job"),
                    "details": f"processed={item.get('processed')}/{item.get('total')}",
                    "created_at": finished,
                }
            )

    def _sort_key(entry: dict[str, Any]) -> datetime:
        raw = entry.get("created_at")
        if not isinstance(raw, str):
            return datetime.min
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min

    timeline.sort(key=_sort_key)
    return timeline
