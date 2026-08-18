from __future__ import annotations

from datetime import datetime, timezone

from jobs.job_models import JobRecord
from jobs.job_repository import JobRepository


class JobService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def snapshot(self) -> dict:
        jobs = self._repository.list_jobs()
        return {
            "items": [self._serialize(job) for job in jobs],
            "meta": {
                "total": len(jobs),
                "running": len([job for job in jobs if job.status == "running"]),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def get_job(self, job_id: str) -> dict | None:
        job = self._repository.get_job(job_id)
        return self._serialize(job) if job else None

    def running(self) -> dict:
        items = self._repository.list_running_jobs()
        return {"items": [self._serialize(job) for job in items]}

    def history(self) -> dict:
        items = self._repository.list_history()
        return {"items": [self._serialize(job) for job in items]}

    @staticmethod
    def _serialize(job: JobRecord) -> dict:
        return {
            "id": job.id,
            "name": job.name,
            "job_type": job.job_type,
            "status": job.status,
            "progress_pct": job.progress_pct,
            "worker": job.worker,
            "cpu_pct": job.cpu_pct,
            "ram_pct": job.ram_pct,
            "eta_seconds": job.eta_seconds,
            "logs": job.logs,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
