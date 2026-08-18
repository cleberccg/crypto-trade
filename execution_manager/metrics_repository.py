from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError

from database.connection import get_session
from database.next_phase_models import ExecutionMetricsModel


class ExecutionMetricsRepository:
    def __init__(self, fallback_file: Path | None = None) -> None:
        if fallback_file is None:
            fallback_file = Path(__file__).resolve().parents[1] / "optimization" / "results" / "execution_metrics.json"
        self.fallback_file = fallback_file
        self.fallback_file.parent.mkdir(parents=True, exist_ok=True)

    def upsert(self, payload: dict[str, Any]) -> None:
        execution_id = str(payload.get("execution_id") or "")
        if not execution_id:
            return

        self._upsert_file(payload)

        try:
            with get_session() as session:
                row = session.execute(
                    select(ExecutionMetricsModel).where(ExecutionMetricsModel.execution_id == execution_id)
                ).scalar_one_or_none()
                if row is None:
                    row = ExecutionMetricsModel(execution_id=execution_id)
                    session.add(row)

                row.status = str(payload.get("status") or row.status)
                row.total_seconds = _as_float(payload.get("total_seconds"))
                row.total_jobs = int(payload.get("total_jobs") or 0)
                row.failed_jobs = int(payload.get("failed_jobs") or 0)
                row.combinations = int(payload.get("combinations") or 0)
                row.combinations_per_second = _as_float(payload.get("combinations_per_second"))
                row.avg_seconds_per_combination = _as_float(payload.get("avg_seconds_per_combination"))
                row.avg_job_seconds = _as_float(payload.get("avg_job_seconds"))
                row.avg_cpu = _as_float(payload.get("avg_cpu"))
                row.max_cpu = _as_float(payload.get("max_cpu"))
                row.avg_ram = _as_float(payload.get("avg_ram"))
                row.max_ram = _as_float(payload.get("max_ram"))
                row.avg_disk = _as_float(payload.get("avg_disk"))
                row.max_disk = _as_float(payload.get("max_disk"))
                row.checkpoints = int(payload.get("checkpoints") or 0)
                row.heartbeats = int(payload.get("heartbeats") or 0)
                row.incidents = int(payload.get("incidents") or 0)
                row.recoveries = int(payload.get("recoveries") or 0)
                row.retries = int(payload.get("retries") or 0)
        except SQLAlchemyError:
            return

    def get(self, execution_id: str) -> dict[str, Any] | None:
        try:
            with get_session() as session:
                row = session.execute(
                    select(ExecutionMetricsModel).where(ExecutionMetricsModel.execution_id == execution_id)
                ).scalar_one_or_none()
                if row is not None:
                    return _to_dict(row)
        except SQLAlchemyError:
            pass

        cache = self._read_file_cache()
        return cache.get(execution_id)

    def latest(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with get_session() as session:
                rows = session.execute(
                    select(ExecutionMetricsModel)
                    .order_by(desc(ExecutionMetricsModel.created_at), desc(ExecutionMetricsModel.id))
                    .limit(limit)
                ).scalars().all()
                if rows:
                    return [_to_dict(row) for row in rows]
        except SQLAlchemyError:
            pass

        cache = self._read_file_cache()
        items = list(cache.values())
        return items[-limit:][::-1]

    def _upsert_file(self, payload: dict[str, Any]) -> None:
        execution_id = str(payload.get("execution_id") or "")
        if not execution_id:
            return
        cache = self._read_file_cache()
        cache[execution_id] = {
            **cache.get(execution_id, {}),
            **payload,
        }
        self.fallback_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_file_cache(self) -> dict[str, dict[str, Any]]:
        if not self.fallback_file.exists():
            return {}
        try:
            payload = json.loads(self.fallback_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
            return {}
        except (OSError, json.JSONDecodeError):
            return {}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_dict(row: ExecutionMetricsModel) -> dict[str, Any]:
    return {
        "execution_id": row.execution_id,
        "status": row.status,
        "total_seconds": row.total_seconds,
        "total_jobs": row.total_jobs,
        "failed_jobs": row.failed_jobs,
        "combinations": row.combinations,
        "combinations_per_second": row.combinations_per_second,
        "avg_seconds_per_combination": row.avg_seconds_per_combination,
        "avg_job_seconds": row.avg_job_seconds,
        "avg_cpu": row.avg_cpu,
        "max_cpu": row.max_cpu,
        "avg_ram": row.avg_ram,
        "max_ram": row.max_ram,
        "avg_disk": row.avg_disk,
        "max_disk": row.max_disk,
        "checkpoints": row.checkpoints,
        "heartbeats": row.heartbeats,
        "incidents": row.incidents,
        "recoveries": row.recoveries,
        "retries": row.retries,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
