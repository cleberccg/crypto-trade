from __future__ import annotations

from pathlib import Path

from execution_manager.execution_models import ExecutionJob, JobStatus
from execution_manager.manager import ExecutionManager


def test_execution_manager_smoke_run(tmp_path: Path) -> None:
    manager = ExecutionManager(tmp_path)

    # Keep smoke test deterministic and fast: avoid real download/optimizer work.
    def _jobs(_execution_id: str) -> list[ExecutionJob]:
        return [
            ExecutionJob(
                name="Smoke Test",
                stage="smoke",
                total=1,
                execution_id=manager.execution_id,
            )
        ]

    def _run_job(job: ExecutionJob) -> ExecutionJob:
        job.status = JobStatus.COMPLETED
        job.processed = job.total
        job.result = {"message": "ok"}
        return job

    manager.scheduler.build_default_jobs = _jobs  # type: ignore[method-assign]
    manager.runner.run_job = _run_job  # type: ignore[method-assign]

    rc = manager.run()
    assert rc in {0, 1}

    results_dir = tmp_path / "optimization" / "results"
    assert (results_dir / "execution_report.json").exists()
    assert (results_dir / "execution_report.txt").exists()
    assert (results_dir / "execution_report.html").exists()
    assert (results_dir / "execution_heartbeat.json").exists()
    assert (results_dir / "execution_jobs_queue.json").exists()
