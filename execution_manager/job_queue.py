from __future__ import annotations

import json
from pathlib import Path

from execution_manager.execution_models import ExecutionJob, JobStatus


class PersistentJobQueue:
    def __init__(self, queue_file: Path) -> None:
        self.queue_file = queue_file
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self._jobs: list[ExecutionJob] = []
        self._load()

    def _load(self) -> None:
        if not self.queue_file.exists():
            self._jobs = []
            return
        payload = json.loads(self.queue_file.read_text(encoding="utf-8"))
        jobs: list[ExecutionJob] = []
        for item in payload:
            job = ExecutionJob(name=item["name"], stage=item["stage"], total=int(item.get("total", 0)))
            job.id = item["id"]
            job.execution_id = item.get("execution_id")
            job.status = JobStatus(item.get("status", JobStatus.WAITING.value))
            job.processed = int(item.get("processed", 0))
            job.eta_seconds = item.get("eta_seconds")
            job.worker = item.get("worker")
            job.cpu = item.get("cpu")
            job.ram = item.get("ram")
            job.result = item.get("result")
            job.error = item.get("error")
            jobs.append(job)
        self._jobs = jobs

    def _save(self) -> None:
        self.queue_file.write_text(json.dumps([job.to_dict() for job in self._jobs], ensure_ascii=False, indent=2), encoding="utf-8")

    def push(self, job: ExecutionJob) -> None:
        self._jobs.append(job)
        self._save()

    def clear(self) -> None:
        self._jobs = []
        self._save()

    def list(self) -> list[ExecutionJob]:
        return list(self._jobs)

    def next_waiting(self) -> ExecutionJob | None:
        for job in self._jobs:
            if job.status == JobStatus.WAITING:
                return job
        return None

    def mark(self, job_id: str, status: JobStatus) -> None:
        for job in self._jobs:
            if job.id == job_id:
                job.status = status
                break
        self._save()
