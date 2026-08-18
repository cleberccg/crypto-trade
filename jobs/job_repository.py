from __future__ import annotations

from collections.abc import Iterable

from jobs.job_models import JobRecord


class JobRepository:
    def __init__(self, jobs: Iterable[JobRecord] | None = None) -> None:
        self._jobs = {job.id: job for job in (jobs or [])}

    def list_jobs(self) -> list[JobRecord]:
        return list(self._jobs.values())

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list_running_jobs(self) -> list[JobRecord]:
        return [job for job in self._jobs.values() if job.status == "running"]

    def list_history(self) -> list[JobRecord]:
        return sorted(self._jobs.values(), key=lambda job: job.updated_at, reverse=True)
