from __future__ import annotations

import threading
import traceback
import json
import os
import platform
import shutil
from statistics import mean
from datetime import datetime, timezone
from pathlib import Path

from execution_manager.execution_logger import ExecutionLoggers
from execution_manager.execution_models import JobStatus
from execution_manager.execution_report import ExecutionReportWriter
from execution_manager.execution_service import ExecutionService
from execution_manager.metrics_repository import ExecutionMetricsRepository
from execution_manager.heartbeat import HeartbeatPublisher
from execution_manager.job_queue import PersistentJobQueue
from execution_manager.progress_tracker import ProgressTracker
from execution_manager.recovery_manager import RecoveryManager
from execution_manager.resource_monitor import ResourceMonitor
from execution_manager.runner import ExecutionRunner
from execution_manager.scheduler import ExecutionScheduler
from execution_manager.watchdog import Watchdog
from notifications.notification_service import get_notification_service


class ExecutionManager:
    def __init__(self, base_dir: Path, execution_id: str | None = None) -> None:
        self.base_dir = base_dir
        self.execution_id = execution_id or ExecutionService.new_execution_id()
        self.service = ExecutionService.create_default(self.execution_id, base_dir)
        self.state = self.service.state

        self.loggers = ExecutionLoggers(base_dir / "logs" / "execution")
        self.queue = PersistentJobQueue(base_dir / "optimization" / "results" / "execution_jobs_queue.json")
        self.scheduler = ExecutionScheduler()
        self.runner = ExecutionRunner()
        self.monitor = ResourceMonitor()
        self.progress = ProgressTracker(self.state)
        self.recovery = RecoveryManager(self.state)
        self.report = ExecutionReportWriter(base_dir / "optimization" / "results")
        self.heartbeat = HeartbeatPublisher(
            state=self.state,
            monitor=self.monitor,
            heartbeat_file=base_dir / "optimization" / "results" / "execution_heartbeat.json",
            interval_seconds=30,
        )
        self.watchdog = Watchdog(self.state)
        self._lock = threading.RLock()
        self._warning_reported = False
        self._run_started_at: datetime | None = None
        self._cpu_samples: list[float] = []
        self._ram_samples: list[float] = []
        self._disk_samples: list[float] = []
        self._heartbeats = 0
        self._checkpoints = 0
        self._incidents = 0
        self._recoveries = 0
        self._retries = 0
        self.metrics_repo = ExecutionMetricsRepository()
        self.notification_service = get_notification_service()

    def _persist_state(self) -> None:
        self.progress.set_db_write()
        self.service.persist()

    def bootstrap_jobs(self) -> None:
        existing = self.queue.list()
        if existing:
            same_execution = all((job.execution_id or "") == self.execution_id for job in existing)
            if not same_execution:
                self.queue.clear()
            else:
                # On restart, convert in-flight jobs back to waiting so the queue can recover.
                for job in existing:
                    if job.status in {JobStatus.RUNNING, JobStatus.RECOVERED, JobStatus.RETRIED}:
                        self.queue.mark(job.id, JobStatus.WAITING)
                return
        for job in self.scheduler.build_default_jobs(self.execution_id):
            self.queue.push(job)

    def run(self) -> int:
        with self._lock:
            self.state.status = "STARTING"
            self.state.started_at = datetime.now(tz=timezone.utc)
            self._run_started_at = self.state.started_at
            self.bootstrap_jobs()
            self.monitor.start()
            self.heartbeat.start()
            self.notification_service.publish(
                "ExecutionStarted",
                "[ExecutionStarted]",
                f"execution_id={self.execution_id}",
                execution_id=self.execution_id,
            )
            self.loggers.execution.info("Execution manager started execution_id=%s", self.execution_id)
            self.state.status = "RUNNING"

        try:
            while True:
                with self._lock:
                    severity, stalled, reason = self.watchdog.evaluate()
                    if severity == "warning":
                        if not self._warning_reported:
                            self.loggers.watchdog.warning("Execution stall warning %.2fs", stalled)
                            self._warning_reported = True
                    elif severity == "critical":
                        self.state.status = "STALLED"
                        self.state.stalled_reason = reason or "unknown"
                        self.loggers.watchdog.error("Execution stall critical %.2fs reason=%s", stalled, reason)
                        self._create_incident("watchdog_critical", RuntimeError(f"stalled_seconds={stalled:.2f} reason={reason}"))
                        self.notification_service.publish(
                            "ExecutionStalled",
                            "[ExecutionStalled]",
                            f"execution_id={self.execution_id} stalled={stalled:.2f}s reason={reason}",
                            execution_id=self.execution_id,
                        )
                        if self._try_recover_stalled():
                            continue
                        break
                    else:
                        self._warning_reported = False

                    job = self.queue.next_waiting()
                    if job is None:
                        failed_jobs = len([j for j in self.queue.list() if j.status == JobStatus.FAILED])
                        self.state.status = "FAILED" if failed_jobs > 0 else "FINALIZING"
                        self.state.finished_at = datetime.now(tz=timezone.utc)
                        if failed_jobs == 0:
                            self.state.status = "COMPLETED"
                            self.notification_service.publish(
                                "ExecutionCompleted",
                                "[ExecutionCompleted]",
                                f"execution_id={self.execution_id}",
                                execution_id=self.execution_id,
                            )
                        else:
                            self.notification_service.publish(
                                "ExecutionFailed",
                                "[ExecutionFailed]",
                                f"execution_id={self.execution_id} failed_jobs={failed_jobs}",
                                execution_id=self.execution_id,
                            )
                        break

                    self.state.current_job_id = job.id
                    self.queue.mark(job.id, JobStatus.RUNNING)

                try:
                    updated = self.runner.run_job(job)
                except KeyboardInterrupt as exc:
                    self.queue.mark(job.id, JobStatus.CANCELLED)
                    self.state.status = "ABANDONED"
                    self._create_incident("keyboard_interrupt", exc)
                    raise
                except Exception as exc:
                    self.queue.mark(job.id, JobStatus.FAILED)
                    self.state.status = "FAILED"
                    self.loggers.errors.exception("Job failed: %s", job.name)
                    self._create_incident("job_failure", exc)
                    self.notification_service.publish(
                        "ExecutionFailed",
                        "[ExecutionFailed]",
                        f"execution_id={self.execution_id} job={job.name} error={type(exc).__name__}",
                        execution_id=self.execution_id,
                    )
                    continue

                with self._lock:
                    self.queue.mark(updated.id, updated.status)
                    total_jobs = len(self.queue.list())
                    completed = len([j for j in self.queue.list() if j.status == JobStatus.COMPLETED])
                    processed_combinations = sum(int(j.processed or 0) for j in self.queue.list())
                    target_combinations = sum(int(j.total or 0) for j in self.queue.list())
                    self.progress.update(processed=processed_combinations, target=max(1, target_combinations))
                    elapsed = max(0.001, (datetime.now(tz=timezone.utc) - self.state.started_at).total_seconds())
                    if processed_combinations > 0:
                        avg_per_combo = elapsed / processed_combinations
                        remaining = max(0, target_combinations - processed_combinations)
                        self.state.eta_seconds = round(remaining * avg_per_combo, 2)
                    else:
                        self.state.eta_seconds = None
                    self.progress.set_checkpoint()
                    self._checkpoints += 1
                    resources = self.monitor.snapshot()
                    self.state.cpu = resources["cpu"]
                    self.state.ram = resources["ram"]
                    self._cpu_samples.append(resources["cpu"])
                    self._ram_samples.append(resources["ram"])
                    self._disk_samples.append(resources.get("disk", 0.0))
                    self.report.write(self.state, self.queue.list())
                    self._persist_state()
                    self.heartbeat.publish_now()
                    self._heartbeats += 1
                    self.loggers.heartbeat.info(
                        "execution_id=%s current_job=%s processed=%s target=%s eta_seconds=%s heartbeat_at=%s",
                        self.execution_id,
                        updated.name,
                        processed_combinations,
                        target_combinations,
                        self.state.eta_seconds,
                        self.state.last_heartbeat_at.isoformat() if self.state.last_heartbeat_at else None,
                    )
                    self.loggers.progress.info(
                        "execution_id=%s job=%s processed=%s target=%s progress=%.2f eta_seconds=%s",
                        self.execution_id,
                        updated.name,
                        processed_combinations,
                        target_combinations,
                        self.state.progress_pct,
                        self.state.eta_seconds,
                    )
                    self.notification_service.publish(
                        "ExecutionProgress",
                        "[ExecutionProgress]",
                        f"execution_id={self.execution_id} progress={self.state.progress_pct}% processed={processed_combinations}/{target_combinations}",
                        execution_id=self.execution_id,
                    )
                    self.loggers.performance.info(
                        "execution_id=%s cpu=%.2f ram=%.2f disk=%.2f",
                        self.execution_id,
                        self.state.cpu,
                        self.state.ram,
                        resources.get("disk", 0.0),
                    )

            return 0 if self.state.status == "COMPLETED" else 1
        finally:
            with self._lock:
                self.heartbeat.stop()
                self.monitor.stop()
                self.report.write(self.state, self.queue.list())
                self._persist_state()
                self._persist_metrics()
                self.loggers.execution.info("Execution manager finished status=%s execution_id=%s", self.state.status, self.execution_id)

    def _try_recover_stalled(self) -> bool:
        with self._lock:
            self.state.status = "RECOVERING"
            self._recoveries += 1
            self.state.recoveries = self._recoveries
            self.progress.set_checkpoint()
            self._persist_state()

        self.notification_service.publish(
            "ExecutionRecovered",
            "[ExecutionRecovered]",
            f"execution_id={self.execution_id} recoveries={self._recoveries}",
            execution_id=self.execution_id,
        )

        # Soft recovery: reset failed/running jobs to waiting so queue can continue from persisted processed.
        with self._lock:
            for job in self.queue.list():
                if job.status in {JobStatus.RUNNING, JobStatus.FAILED}:
                    self.queue.mark(job.id, JobStatus.RECOVERED)
                    self.queue.mark(job.id, JobStatus.WAITING)
            self.state.status = "RUNNING"
            self._persist_state()
        return True

    def _create_incident(self, incident_type: str, exc: BaseException) -> None:
        self._incidents += 1
        now = datetime.now(tz=timezone.utc)
        incident_root = self.base_dir / "optimization" / "results" / "incidents"
        incident_dir = incident_root / f"INC_{now.strftime('%Y%m%d_%H%M%S')}"
        incident_dir.mkdir(parents=True, exist_ok=True)

        stacktrace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        execution_state = self.state.to_dict()
        jobs = [j.to_dict() for j in self.queue.list()]

        incident_json = {
            "incident_type": incident_type,
            "execution_id": self.execution_id,
            "timestamp": now.isoformat(),
            "status": self.state.status,
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        }
        (incident_dir / "incident.json").write_text(json.dumps(incident_json, ensure_ascii=False, indent=2), encoding="utf-8")
        (incident_dir / "stacktrace.txt").write_text(stacktrace, encoding="utf-8")
        (incident_dir / "execution_state.json").write_text(json.dumps(execution_state, ensure_ascii=False, indent=2), encoding="utf-8")
        (incident_dir / "jobs_state.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        (incident_dir / "checkpoint_reference.txt").write_text(
            f"execution_id={self.execution_id}\nlast_checkpoint={execution_state.get('last_checkpoint_at')}\n",
            encoding="utf-8",
        )

        env_lines = [
            f"platform={platform.platform()}",
            f"python={platform.python_version()}",
            f"cwd={os.getcwd()}",
            f"execution_id={self.execution_id}",
        ]
        (incident_dir / "environment.txt").write_text("\n".join(env_lines), encoding="utf-8")

        logs_dir = self.base_dir / "logs" / "execution"
        if logs_dir.exists():
            zip_base = incident_dir / "logs"
            shutil.make_archive(str(zip_base), "zip", root_dir=str(logs_dir))

    def pause(self) -> dict[str, str]:
        with self._lock:
            self.state.status = "Paused"
            self._persist_state()
        return {"status": "paused"}

    def resume(self) -> dict[str, str]:
        with self._lock:
            self.state.status = "Running"
            self._persist_state()
        return {"status": "running"}

    def cancel(self) -> dict[str, str]:
        with self._lock:
            self.state.status = "Cancelled"
            self.state.finished_at = datetime.now(tz=timezone.utc)
            self._persist_state()
        return {"status": "cancelled"}

    def retry_failed(self) -> dict[str, str]:
        with self._lock:
            for job in self.queue.list():
                if job.status == JobStatus.FAILED:
                    self.queue.mark(job.id, JobStatus.RETRIED)
                    self.queue.mark(job.id, JobStatus.WAITING)
                    self._retries += 1
            self._persist_state()
        return {"status": "retried"}

    def _persist_metrics(self) -> None:
        if self._run_started_at is None:
            return

        finished_at = self.state.finished_at or datetime.now(tz=timezone.utc)
        total_seconds = max(0.0, (finished_at - self._run_started_at).total_seconds())
        jobs = self.queue.list()
        total_jobs = len(jobs)
        failed_jobs = len([j for j in jobs if j.status == JobStatus.FAILED])
        combinations = sum(int(j.total or 0) for j in jobs)
        combos_per_sec = (combinations / total_seconds) if total_seconds > 0 else None
        avg_sec_combo = (total_seconds / combinations) if combinations > 0 else None
        avg_job_seconds = (total_seconds / total_jobs) if total_jobs > 0 else None

        payload = {
            "execution_id": self.execution_id,
            "status": self.state.status,
            "total_seconds": total_seconds,
            "total_jobs": total_jobs,
            "failed_jobs": failed_jobs,
            "combinations": combinations,
            "combinations_per_second": combos_per_sec,
            "avg_seconds_per_combination": avg_sec_combo,
            "avg_job_seconds": avg_job_seconds,
            "avg_cpu": mean(self._cpu_samples) if self._cpu_samples else None,
            "max_cpu": max(self._cpu_samples) if self._cpu_samples else None,
            "avg_ram": mean(self._ram_samples) if self._ram_samples else None,
            "max_ram": max(self._ram_samples) if self._ram_samples else None,
            "avg_disk": mean(self._disk_samples) if self._disk_samples else None,
            "max_disk": max(self._disk_samples) if self._disk_samples else None,
            "checkpoints": self._checkpoints,
            "heartbeats": self._heartbeats,
            "incidents": self._incidents,
            "recoveries": self._recoveries,
            "retries": self._retries,
        }
        self.metrics_repo.upsert(payload)

    def snapshot(self) -> dict:
        with self._lock:
            items = self.queue.list()
            waiting = len([j for j in items if j.status == JobStatus.WAITING])
            running = len([j for j in items if j.status == JobStatus.RUNNING])
            failed = len([j for j in items if j.status == JobStatus.FAILED])
            return {
                "execution": self.state.to_dict(),
                "jobs": [j.to_dict() for j in items],
                "queue": {
                    "waiting": waiting,
                    "running": running,
                    "failed": failed,
                    "total": len(items),
                },
                "performance": self.monitor.snapshot(),
            }
