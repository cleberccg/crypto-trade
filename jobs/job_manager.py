from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobs.job_models import JobRecord
from jobs.job_repository import JobRepository
from jobs.job_service import JobService


class JobManager:
    def __init__(self) -> None:
        self.repository = JobRepository(self._seed_jobs())
        self.service = JobService(self.repository)

    def _seed_jobs(self) -> list[JobRecord]:
        now = datetime.now(timezone.utc)
        return [
            JobRecord(
                id="job-download-001",
                name="Download BTC/USDT",
                job_type="download",
                status="completed",
                progress_pct=100.0,
                worker="worker-1",
                cpu_pct=12.5,
                ram_pct=41.2,
                eta_seconds=0,
                logs=["Download iniciado", "Download concluído"],
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1, minutes=50),
            ),
            JobRecord(
                id="job-optimizer-001",
                name="Optimizer TrendV1",
                job_type="optimizer",
                status="running",
                progress_pct=63.4,
                worker="worker-4",
                cpu_pct=82.1,
                ram_pct=73.8,
                eta_seconds=19800,
                logs=["Optimizer iniciado", "Checkpoint salvo", "Novo melhor PF"],
                created_at=now - timedelta(hours=6),
                updated_at=now - timedelta(minutes=2),
            ),
            JobRecord(
                id="job-research-001",
                name="Research Lab Sync",
                job_type="research",
                status="queued",
                progress_pct=0.0,
                worker=None,
                cpu_pct=0.0,
                ram_pct=0.0,
                eta_seconds=0,
                logs=["Aguardando termino do processamento principal"],
                created_at=now,
                updated_at=now,
            ),
        ]
