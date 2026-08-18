"""Night runner orchestration for long unattended optimization sessions.

This module prepares and executes a complete overnight workflow with
pre-flight checks, incremental downloads, smoke test, full optimization
sequence, backups, resume support, and final reporting.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil

from config.settings import settings
from database.bootstrap import bootstrap_database
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.next_phase_models import ExecutionTimelineEvent
from database.models import Candle
from database.repositories import CandleRepository
from exchange.binance_client import BinanceClient
from exchange.binance_market_data_client import BinanceMarketDataClient
from exchange.data_downloader import DataDownloader
from utils.logger import setup_logger

ROOT = Path(__file__).parent
NIGHT_LOG_DIR = ROOT / "logs" / "night"
BACKUP_DIR = ROOT / "backups"
STATE_FILE = ROOT / "optimization" / "results" / "night_runner_state.json"
REPORT_FILE = ROOT / "optimization" / "results" / "night_execution_report.txt"
INCIDENT_FILE = ROOT / "optimization" / "results" / "incident_report.txt"
HEARTBEAT_FILE = ROOT / "optimization" / "results" / "night_runner_heartbeat.json"

DOWNLOAD_START_DATE = "2024-01-01"
SMOKE_MAX_COMBINATIONS = 500
SMOKE_WORKERS = 16
MAIN_MAX_COMBINATIONS = 10000
MAIN_WORKERS = 16

TARGETS = [
    ("BTC/USDT", "5m"),
    ("BTC/USDT", "15m"),
    ("BTC/USDT", "30m"),
    ("BTC/USDT", "1h"),
    ("BTC/USDT", "4h"),
    ("ETH/USDT", "5m"),
    ("ETH/USDT", "15m"),
    ("ETH/USDT", "30m"),
    ("ETH/USDT", "1h"),
    ("ETH/USDT", "4h"),
    ("SOL/USDT", "5m"),
    ("SOL/USDT", "15m"),
    ("SOL/USDT", "30m"),
    ("SOL/USDT", "1h"),
    ("SOL/USDT", "4h"),
    ("BNB/USDT", "5m"),
    ("BNB/USDT", "15m"),
    ("BNB/USDT", "30m"),
    ("BNB/USDT", "1h"),
    ("BNB/USDT", "4h"),
    ("XRP/USDT", "5m"),
    ("XRP/USDT", "15m"),
    ("XRP/USDT", "30m"),
    ("XRP/USDT", "1h"),
    ("XRP/USDT", "4h"),
    ("LINK/USDT", "5m"),
    ("LINK/USDT", "15m"),
    ("LINK/USDT", "30m"),
    ("LINK/USDT", "1h"),
    ("LINK/USDT", "4h"),
    ("AVAX/USDT", "5m"),
    ("AVAX/USDT", "15m"),
    ("AVAX/USDT", "30m"),
    ("AVAX/USDT", "1h"),
    ("AVAX/USDT", "4h"),
    ("DOGE/USDT", "5m"),
    ("DOGE/USDT", "15m"),
    ("DOGE/USDT", "30m"),
    ("DOGE/USDT", "1h"),
    ("DOGE/USDT", "4h"),
]


@dataclass
class StepResult:
    name: str
    ok: bool
    started_at: datetime
    finished_at: datetime
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class ResourceMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cpu_samples: list[float] = []
        self.ram_samples: list[float] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.cpu_samples.append(psutil.cpu_percent(interval=1))
            self.ram_samples.append(psutil.virtual_memory().percent)

    def averages(self) -> tuple[float, float]:
        cpu = sum(self.cpu_samples) / len(self.cpu_samples) if self.cpu_samples else 0.0
        ram = sum(self.ram_samples) / len(self.ram_samples) if self.ram_samples else 0.0
        return cpu, ram


class ProgressTracker:
    def __init__(self) -> None:
        self.started_at = datetime.now(tz=timezone.utc)
        self.last_checkpoint_at: datetime | None = None
        self.last_log_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_db_update_at: datetime | None = None
        self.last_stage: str | None = None
        self.last_combo: str | None = None
        self.last_processed: int = 0
        self.state: str = "idle"

    def touch_log(self) -> None:
        self.last_log_at = datetime.now(tz=timezone.utc)

    def touch_checkpoint(self, stage: str, processed: int) -> None:
        now = datetime.now(tz=timezone.utc)
        self.last_checkpoint_at = now
        self.last_db_update_at = now
        self.last_stage = stage
        self.last_processed = processed

    def touch_heartbeat(self, stage: str | None = None, combo: str | None = None, state: str | None = None) -> None:
        now = datetime.now(tz=timezone.utc)
        self.last_heartbeat_at = now
        if stage is not None:
            self.last_stage = stage
        if combo is not None:
            self.last_combo = combo
        if state is not None:
            self.state = state

    def stalled_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(tz=timezone.utc)
        candidates = [ts for ts in [self.last_checkpoint_at, self.last_log_at, self.last_db_update_at, self.last_heartbeat_at] if ts is not None]
        if not candidates:
            return (current - self.started_at).total_seconds()
        latest = max(candidates)
        return max(0.0, (current - latest).total_seconds())

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "last_checkpoint_at": self.last_checkpoint_at.isoformat() if self.last_checkpoint_at else None,
            "last_log_at": self.last_log_at.isoformat() if self.last_log_at else None,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
            "last_db_update_at": self.last_db_update_at.isoformat() if self.last_db_update_at else None,
            "last_stage": self.last_stage,
            "last_combo": self.last_combo,
            "last_processed": self.last_processed,
            "state": self.state,
            "stalled_seconds": self.stalled_seconds(),
        }


class NightRunner:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        NIGHT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (ROOT / "optimization" / "results").mkdir(parents=True, exist_ok=True)

        self.night_logger = setup_logger("night_runner", log_dir=NIGHT_LOG_DIR, log_to_file=True)
        self.download_logger = setup_logger("downloads", log_dir=NIGHT_LOG_DIR, log_to_file=True)
        self.optimizer_logger = setup_logger("optimizer", log_dir=NIGHT_LOG_DIR, log_to_file=True)
        self.validation_logger = setup_logger("validation", log_dir=NIGHT_LOG_DIR, log_to_file=True)
        self.error_logger = setup_logger("errors", log_dir=NIGHT_LOG_DIR, log_to_file=True)

        self.monitor = ResourceMonitor()
        self.progress = ProgressTracker()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._abort_execution = threading.Event()
        self.step_results: list[StepResult] = []
        self.total_downloads = 0
        self.total_optimizations = 0
        self.total_validations = 0
        self.total_combinations = 0
        self.total_approved = 0
        self.total_rejected = 0
        self.best_profit_factor = float("-inf")
        self.best_configuration: dict[str, Any] | None = None
        self.best_asset = ""
        self.best_timeframe = ""
        self.errors = 0
        self.incident: dict[str, Any] | None = None

    def run(self) -> int:
        started = datetime.now(tz=timezone.utc)
        self.monitor.start()
        self._start_heartbeat()
        self._start_watchdog()
        self.night_logger.info("Night runner started at %s", started.isoformat())
        self.progress.touch_heartbeat(state="starting")
        self._emit_timeline_event("execution_started", "Execucao iniciada", "Night runner iniciado")

        try:
            preflight = self._step("preflight", self._preflight_checks)
            if not preflight.ok:
                self.night_logger.error("Preflight failed. Aborting execution.")
                self.progress.state = "failed"
                return 1

            self._step("downloads", self._run_downloads)

            smoke = self._step("smoke", self._run_smoke)
            if not smoke.ok:
                self.night_logger.error("Smoke test failed. Aborting execution.")
                self.progress.state = "failed"
                return 1

            self._step("main_pipeline", self._run_main_pipeline)
            self.progress.state = "completed"
            self._emit_timeline_event("execution_finished", "Execucao finalizada", "Night runner concluido com sucesso")
            return 0
        except Exception as exc:
            self.progress.state = "failed"
            self._record_incident("unexpected_failure", exc)
            self._emit_timeline_event("execution_failed", "Execucao falhou", str(exc))
            raise
        finally:
            finished = datetime.now(tz=timezone.utc)
            self._stop_watchdog()
            self._stop_heartbeat()
            self.monitor.stop()
            self._write_heartbeat(final=True)
            self._write_incident_report()
            self._write_report(started, finished)
            self.night_logger.info("Night runner finished at %s", finished.isoformat())

    def _step(self, name: str, func) -> StepResult:
        start = datetime.now(tz=timezone.utc)
        ok = True
        details: dict[str, Any] = {}
        try:
            details = func() or {}
        except Exception as exc:  # pragma: no cover - runtime safety
            ok = False
            details = {"error": str(exc)}
            self.errors += 1
            self.error_logger.exception("Step %s failed: %s", name, exc)
            self._record_incident(name, exc)
        end = datetime.now(tz=timezone.utc)
        result = StepResult(name=name, ok=ok, started_at=start, finished_at=end, details=details)
        self.step_results.append(result)
        self.night_logger.info("Step %s finished ok=%s duration=%.2fs", name, ok, result.duration_seconds)
        self.progress.touch_log()
        return result

    def _preflight_checks(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}

        db_url = settings.database.url
        checks["database_url"] = db_url
        bootstrap_database()
        if "sqlite" in db_url:
            sqlite_path = self._resolve_sqlite_path(db_url)
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute("PRAGMA integrity_check").fetchone()
            checks["sqlite"] = str(sqlite_path)
        else:
            checks["database_backend"] = settings.database.type

        env_path = ROOT / ".env"
        if not env_path.exists():
            raise RuntimeError(".env file not found")
        checks["env_loaded"] = True

        free_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
        checks["disk_free_gb"] = round(free_gb, 2)
        if free_gb < 10:
            raise RuntimeError(f"Low disk space: {free_gb:.2f} GB")

        required_dirs = [
            ROOT / "optimization" / "results",
            ROOT / "backtesting" / "results",
            ROOT / "logs",
            NIGHT_LOG_DIR,
            BACKUP_DIR,
        ]
        for directory in required_dirs:
            directory.mkdir(parents=True, exist_ok=True)
        checks["directories_ok"] = True

        workers = int(os.getenv("OPTIMIZER_WORKERS", str(settings.optimizer.workers)))
        if workers < 1:
            raise RuntimeError("Invalid OPTIMIZER_WORKERS")
        checks["workers"] = workers

        client = BinanceClient()
        try:
            client.connect()
            ticker = client.fetch_ticker("BTC/USDT")
            if not ticker or ticker.get("symbol") is None:
                raise RuntimeError("Binance ticker response invalid")
            checks["binance_ok"] = True
        finally:
            client.disconnect()

        with get_session() as session:
            session.execute(Candle.__table__.select().limit(1))
        checks["database_consistency"] = True
        return checks

    def _run_downloads(self) -> dict[str, Any]:
        client = BinanceMarketDataClient()
        client.connect()
        downloader = DataDownloader(client)

        downloaded: list[dict[str, Any]] = []
        try:
            for symbol, timeframe in TARGETS:
                start = datetime.fromisoformat(f"{DOWNLOAD_START_DATE}T00:00:00+00:00")
                end = datetime.now(tz=timezone.utc)
                missing_start = self._compute_missing_start(symbol, timeframe, start)
                if missing_start >= end:
                    self.download_logger.info("%s %s already up to date", symbol, timeframe)
                    downloaded.append({"symbol": symbol, "timeframe": timeframe, "inserted": 0})
                    continue

                before = self._count_candles(symbol, timeframe)
                downloader.download_historical(symbol, timeframe, missing_start, end)
                after = self._count_candles(symbol, timeframe)
                inserted = max(0, after - before)
                self.total_downloads += inserted
                downloaded.append({"symbol": symbol, "timeframe": timeframe, "inserted": inserted})
                self.download_logger.info("Download complete %s %s inserted=%d", symbol, timeframe, inserted)
                self._write_heartbeat(stage=f"download:{symbol}:{timeframe}", combo=None, state="running")
        finally:
            client.disconnect()
        return {"targets": downloaded}

    def _run_smoke(self) -> dict[str, Any]:
        window = self._build_validation_window("BTC/USDT", "5m", "2024-01-01")
        cmd = self._optimize_cmd(
            symbol="BTC/USDT",
            timeframe="5m",
            start="2024-01-01",
            max_combinations=SMOKE_MAX_COMBINATIONS,
            workers=SMOKE_WORKERS,
            end=window["end"],
            train_start=window["train_start"],
            train_end=window["train_end"],
            val_start=window["val_start"],
            val_end=window["val_end"],
        )
        self.night_logger.info("[SMOKE] running: %s", " ".join(cmd))
        proc = self._run_command(
            cmd,
            self.optimizer_logger,
            progress_total=SMOKE_MAX_COMBINATIONS,
            progress_label="[SMOKE] BTC/USDT 5m",
        )
        if proc.returncode != 0:
            raise RuntimeError("Smoke optimize failed")

        # Validation runs inside optimize command; if optimize succeeded smoke is approved for continuity.
        self.total_optimizations += 1
        self.total_validations += 1
        self.total_combinations += SMOKE_MAX_COMBINATIONS
        self._write_checkpoint("smoke", SMOKE_MAX_COMBINATIONS, completed=True, payload={"symbol": "BTC/USDT", "timeframe": "5m"})
        return {"return_code": proc.returncode}

    def _run_main_pipeline(self) -> dict[str, Any]:
        state = self._load_state()
        sequence = [(sym, tf) for sym, tf in TARGETS]

        for idx, (symbol, timeframe) in enumerate(sequence, start=1):
            if self._abort_execution.is_set():
                raise RuntimeError("Night runner aborted by watchdog due to detected stall")

            stage_key = f"{symbol}_{timeframe}".replace("/", "_")
            if state.get("completed", {}).get(stage_key):
                self.night_logger.info("Skipping completed stage %s", stage_key)
                continue

            self.night_logger.info("[%d/%d] %s %s", idx, len(sequence), symbol, timeframe)
            self._progress(idx, len(sequence), 0, symbol, timeframe)

            last_exec = state.get("last_execution_id", {}).get(stage_key)
            if not last_exec:
                last_exec = self._find_resume_execution_id(symbol, timeframe)
                if last_exec:
                    state.setdefault("last_execution_id", {})[stage_key] = last_exec
                    self._save_state(state)
                    self._emit_timeline_event(
                        "recovery_resumed",
                        "Execucao retomada",
                        f"Retomada de checkpoint valida para {symbol} {timeframe} ({last_exec})",
                    )
            cmd = self._optimize_cmd(
                symbol=symbol,
                timeframe=timeframe,
                start="2024-01-01",
                max_combinations=MAIN_MAX_COMBINATIONS,
                workers=MAIN_WORKERS,
                resume_execution_id=last_exec,
                **self._build_validation_window(symbol, timeframe, "2024-01-01"),
            )

            proc = self._run_command(
                cmd,
                self.optimizer_logger,
                progress_total=MAIN_MAX_COMBINATIONS,
                progress_label=f"[{idx}/{len(sequence)}] {symbol} {timeframe}",
                stage_name=stage_key,
                checkpoint_interval=settings.optimizer.checkpoint_interval,
            )
            if proc.returncode != 0:
                self.error_logger.error("Optimization failed for %s %s", symbol, timeframe)
                self.errors += 1
                latest_failed_exec = self._latest_optimization_execution_id(symbol, timeframe)
                if latest_failed_exec:
                    state.setdefault("last_execution_id", {})[stage_key] = latest_failed_exec
                    self._save_state(state)
                # Continue to next stage by requirement.
                continue

            self.total_optimizations += 1
            self.total_validations += 1
            self.total_combinations += MAIN_MAX_COMBINATIONS

            execution_id = self._latest_optimization_execution_id(symbol, timeframe)
            if execution_id:
                state.setdefault("last_execution_id", {})[stage_key] = execution_id

            self._collect_validation_stats(execution_id)
            self._collect_best_result(execution_id)
            self._create_backup(symbol, timeframe, execution_id)

            state.setdefault("completed", {})[stage_key] = True
            self._save_state(state)
            self._write_checkpoint(stage_key, MAIN_MAX_COMBINATIONS, completed=True, payload={"symbol": symbol, "timeframe": timeframe, "execution_id": execution_id})
            self._progress(idx, len(sequence), 100, symbol, timeframe)

        return {"completed": len(state.get("completed", {}))}

    def _find_resume_execution_id(self, symbol: str, timeframe: str) -> str | None:
        from database.history_models import ExecutionCheckpoint, OptimizationRun
        from sqlalchemy import desc

        with get_session() as session:
            latest_run = (
                session.query(OptimizationRun)
                .filter_by(symbol=symbol, timeframe=timeframe)
                .order_by(desc(OptimizationRun.id))
                .first()
            )
            if latest_run is None:
                return None

            checkpoint = (
                session.query(ExecutionCheckpoint)
                .filter_by(execution_id=latest_run.execution_id, stage="optimizer")
                .order_by(desc(ExecutionCheckpoint.created_at), desc(ExecutionCheckpoint.id))
                .first()
            )
            if checkpoint is None:
                return None
            if checkpoint.completed:
                return None
            is_valid, reason = self._validate_resume_checkpoint(latest_run.execution_id, symbol, timeframe)
            if not is_valid:
                self.error_logger.error(
                    "Checkpoint rejected for execution_id=%s symbol=%s timeframe=%s reason=%s",
                    latest_run.execution_id,
                    symbol,
                    timeframe,
                    reason,
                )
                self._emit_timeline_event(
                    "checkpoint_invalid",
                    "Checkpoint invalido",
                    f"{symbol} {timeframe} execution_id={latest_run.execution_id} motivo={reason}",
                )
                return None
            return latest_run.execution_id

    def _validate_resume_checkpoint(self, execution_id: str, symbol: str, timeframe: str) -> tuple[bool, str]:
        from database.history_models import ExecutionCheckpoint, OptimizationResultRecord, OptimizationRun
        from sqlalchemy import desc

        try:
            with get_session() as session:
                run = session.query(OptimizationRun).filter_by(execution_id=execution_id).first()
                if run is None:
                    return False, "optimization_run_not_found"
                if run.symbol != symbol or run.timeframe != timeframe:
                    return False, "symbol_or_timeframe_mismatch"
                if str(run.status).lower() == "completed":
                    return False, "already_completed"

                checkpoint = (
                    session.query(ExecutionCheckpoint)
                    .filter_by(execution_id=execution_id, stage="optimizer")
                    .order_by(desc(ExecutionCheckpoint.created_at), desc(ExecutionCheckpoint.id))
                    .first()
                )
                if checkpoint is None:
                    return False, "checkpoint_not_found"
                if checkpoint.completed:
                    return False, "checkpoint_already_completed"
                if checkpoint.processed < 0:
                    return False, "checkpoint_negative_processed"

                persisted_results = session.query(OptimizationResultRecord).filter_by(execution_id=execution_id).count()
                if persisted_results < checkpoint.processed:
                    return False, "persisted_results_less_than_checkpoint"

                if "sqlite" in settings.database.url:
                    sqlite_path = self._resolve_sqlite_path(settings.database.url)
                    with sqlite3.connect(sqlite_path) as conn:
                        row = conn.execute("PRAGMA quick_check").fetchone()
                        if row is None or str(row[0]).lower() != "ok":
                            return False, "sqlite_quick_check_failed"
        except Exception as exc:
            return False, f"validation_exception:{exc}"

        return True, "ok"

    def _create_backup(self, symbol: str, timeframe: str, execution_id: str | None) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        target = BACKUP_DIR / timestamp
        suffix = f"_{symbol.replace('/', '-')}_{timeframe}"
        if target.exists():
            target = BACKUP_DIR / f"{timestamp}{suffix}"
        target.mkdir(parents=True, exist_ok=True)

        files_to_copy = [
            ROOT / "optimization" / "results" / "optimization_results.csv",
            ROOT / "optimization" / "results" / "optimization_results.json",
            ROOT / "optimization" / "results" / "optimization_report.txt",
            ROOT / "optimization" / "results" / "validation_report.csv",
            ROOT / "optimization" / "results" / "validation_report.json",
            ROOT / "optimization" / "results" / "validation_report.txt",
        ]
        if "sqlite" in settings.database.url:
            files_to_copy.insert(0, self._resolve_sqlite_path(settings.database.url))

        for path in files_to_copy:
            if path.exists() and path.is_file():
                shutil.copy2(path, target / path.name)

        logs_target = target / "logs"
        logs_target.mkdir(parents=True, exist_ok=True)
        for log_file in NIGHT_LOG_DIR.glob("*.log"):
            shutil.copy2(log_file, logs_target / log_file.name)

        metadata = {
            "symbol": symbol,
            "timeframe": timeframe,
            "execution_id": execution_id,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        (target / "backup_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self.night_logger.info("Backup created at %s", target)

    def _write_report(self, started: datetime, finished: datetime) -> None:
        total_time = (finished - started).total_seconds()
        cpu_avg, ram_avg = self.monitor.averages()
        avg_per_comb = self.total_time_for("main_pipeline") / max(1, self.total_combinations)

        lines = [
            "Night Execution Report",
            "=" * 60,
            f"Started: {started.isoformat()}",
            f"Finished: {finished.isoformat()}",
            f"Tempo total (s): {total_time:.2f}",
            "",
            "Tempo por etapa:",
        ]
        for step in self.step_results:
            lines.append(f"- {step.name}: {step.duration_seconds:.2f}s (ok={step.ok})")

        lines.extend(
            [
                "",
                f"Quantidade de downloads (candles inseridos): {self.total_downloads}",
                f"Quantidade de otimizacoes: {self.total_optimizations}",
                f"Quantidade de validacoes: {self.total_validations}",
                f"Quantidade de combinacoes testadas (estimada): {self.total_combinations}",
                f"Quantidade de estrategias aprovadas: {self.total_approved}",
                f"Quantidade de estrategias rejeitadas: {self.total_rejected}",
                f"Melhor Profit Factor: {self.best_profit_factor if self.best_profit_factor != float('-inf') else 0}",
                f"Melhor configuracao: {json.dumps(self.best_configuration, ensure_ascii=False) if self.best_configuration else '{}'}",
                f"Melhor ativo: {self.best_asset}",
                f"Melhor timeframe: {self.best_timeframe}",
                f"Quantidade de erros: {self.errors}",
                f"Tempo medio por combinacao (s): {avg_per_comb:.6f}",
                f"Uso medio de CPU (%): {cpu_avg:.2f}",
                f"Uso medio de RAM (%): {ram_avg:.2f}",
            ]
        )

        REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")

    def _run_command(
        self,
        cmd: list[str],
        logger_obj,
        progress_total: int | None = None,
        progress_label: str | None = None,
        stage_name: str | None = None,
        checkpoint_interval: int | None = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        milestones = [25, 50, 75, 100]
        reached: set[int] = set()
        pattern = re.compile(r"processed=(\d+)")

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stderr_queue: queue.Queue[str | None] = queue.Queue()

        def _pump(stream, out_queue: queue.Queue[str | None]) -> None:
            assert stream is not None
            for line in iter(stream.readline, ""):
                out_queue.put(line.rstrip())
            out_queue.put(None)

        stdout_thread = threading.Thread(target=_pump, args=(process.stdout, stdout_queue), daemon=True)
        stderr_thread = threading.Thread(target=_pump, args=(process.stderr, stderr_queue), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        stdout_done = False
        stderr_done = False
        last_checkpoint_count = 0

        while not (stdout_done and stderr_done):
            if self._abort_execution.is_set():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise RuntimeError("Subprocess aborted by watchdog due to detected stall")

            try:
                line = stdout_queue.get(timeout=0.5)
            except queue.Empty:
                line = None
            if line is None:
                stdout_done = stdout_done or process.poll() is not None
            else:
                stdout_lines.append(line)
                logger_obj.info(line)
                self.progress.touch_log()
                self.progress.touch_heartbeat(stage=stage_name, combo=progress_label, state="running")

                if progress_total is not None:
                    match = pattern.search(line)
                    if match:
                        processed = int(match.group(1))
                        pct = min(100, int((processed / max(1, progress_total)) * 100))
                        for milestone in milestones:
                            if pct >= milestone and milestone not in reached:
                                reached.add(milestone)
                                self._log_progress_marker(progress_label or "stage", milestone)
                        if stage_name and checkpoint_interval and processed - last_checkpoint_count >= checkpoint_interval:
                            last_checkpoint_count = processed
                            self._write_checkpoint(stage_name, processed, completed=False, payload={"label": progress_label, "milestone": pct})
                            self._write_heartbeat(stage=stage_name, combo=progress_label, state="running")

            while True:
                try:
                    err_line = stderr_queue.get_nowait()
                except queue.Empty:
                    break
                if err_line is None:
                    stderr_done = stderr_done or process.poll() is not None
                    break
                stderr_lines.append(err_line)
                logger_obj.error(err_line)
                self.progress.touch_log()

            if process.poll() is not None and stdout_done and stderr_done:
                break

        return_code = process.wait()

        if progress_total is not None and 100 not in reached and return_code == 0:
            self._log_progress_marker(progress_label or "stage", 100)
        if stage_name:
            self._write_checkpoint(stage_name, progress_total or 0, completed=return_code == 0, payload={"return_code": return_code, "label": progress_label})

        return subprocess.CompletedProcess(cmd, return_code, "\n".join(stdout_lines), "\n".join(stderr_lines))

    def _log_progress_marker(self, label: str, milestone: int) -> None:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.night_logger.info("%s | %d%% | CPU %.1f%% | RAM %.1f%%", label, milestone, cpu, ram)

    def _optimize_cmd(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        max_combinations: int,
        workers: int,
        resume_execution_id: str | None = None,
        end: str | None = None,
        train_start: str | None = None,
        train_end: str | None = None,
        val_start: str | None = None,
        val_end: str | None = None,
    ) -> list[str]:
        cmd = [
            sys.executable,
            "main.py",
            "optimize",
            "--symbol",
            symbol,
            "--timeframe",
            timeframe,
            "--start",
            start,
            "--max-combinations",
            str(max_combinations),
            "--workers",
            str(workers),
            "--top",
            "10",
        ]
        if end:
            cmd.extend(["--end", end])
        if train_start:
            cmd.extend(["--train-start", train_start])
        if train_end:
            cmd.extend(["--train-end", train_end])
        if val_start:
            cmd.extend(["--val-start", val_start])
        if val_end:
            cmd.extend(["--val-end", val_end])
        if resume_execution_id:
            cmd.extend(["--resume-execution-id", resume_execution_id])
        return cmd

    def _resolve_sqlite_path(self, db_url: str) -> Path:
        if not db_url.startswith("sqlite:///"):
            raise RuntimeError("DATABASE_URL is not sqlite")
        raw = db_url.replace("sqlite:///", "", 1)
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        return path

    def _compute_missing_start(self, symbol: str, timeframe: str, requested_start: datetime) -> datetime:
        with get_session() as session:
            repo = CandleRepository(session)
            latest = repo.get_latest(symbol, timeframe)
        if latest is None:
            return requested_start

        latest_open_time = latest.open_time
        if latest_open_time.tzinfo is None:
            latest_open_time = latest_open_time.replace(tzinfo=timezone.utc)

        delta_minutes = 5
        if timeframe.endswith("m"):
            delta_minutes = int(timeframe[:-1])
        elif timeframe.endswith("h"):
            delta_minutes = int(timeframe[:-1]) * 60

        requested_aware = requested_start
        if requested_aware.tzinfo is None:
            requested_aware = requested_aware.replace(tzinfo=timezone.utc)

        return max(requested_aware, latest_open_time + timedelta(minutes=delta_minutes))

    def _build_validation_window(self, symbol: str, timeframe: str, fallback_start: str) -> dict[str, str]:
        with get_session() as session:
            rows = (
                session.query(Candle.open_time)
                .filter_by(symbol=symbol, timeframe=timeframe)
                .order_by(Candle.open_time.asc())
                .all()
            )

        if not rows:
            today = datetime.now(tz=timezone.utc).date().isoformat()
            return {
                "end": today,
                "train_start": fallback_start,
                "train_end": fallback_start,
                "val_start": fallback_start,
                "val_end": today,
            }

        start_dt = rows[0][0]
        end_dt = rows[-1][0]
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        total_seconds = max(1, (end_dt - start_dt).total_seconds())
        split_dt = start_dt + timedelta(seconds=int(total_seconds * 0.7))
        if split_dt >= end_dt:
            split_dt = end_dt - timedelta(minutes=5)

        train_start = start_dt.date().isoformat()
        train_end = split_dt.date().isoformat()
        val_start = split_dt.date().isoformat()
        val_end = end_dt.date().isoformat()

        return {
            "end": val_end,
            "train_start": train_start,
            "train_end": train_end,
            "val_start": val_start,
            "val_end": val_end,
        }

    def _count_candles(self, symbol: str, timeframe: str) -> int:
        with get_session() as session:
            return session.query(Candle).filter_by(symbol=symbol, timeframe=timeframe).count()

    def _latest_optimization_execution_id(self, symbol: str, timeframe: str) -> str | None:
        from database.history_models import OptimizationRun
        from sqlalchemy import desc

        with get_session() as session:
            row = (
                session.query(OptimizationRun)
                .filter_by(symbol=symbol, timeframe=timeframe)
                .order_by(desc(OptimizationRun.id))
                .first()
            )
            return row.execution_id if row else None

    def _collect_validation_stats(self, execution_id: str | None) -> None:
        if execution_id is None:
            return
        from database.history_models import ValidationRun
        from sqlalchemy import desc

        with get_session() as session:
            row = (
                session.query(ValidationRun)
                .filter_by(optimizer_run=execution_id)
                .order_by(desc(ValidationRun.id))
                .first()
            )
            if row:
                self.total_approved += int(row.approved)
                self.total_rejected += int(row.rejected)

    def _collect_best_result(self, execution_id: str | None) -> None:
        if execution_id is None:
            return
        from database.history_models import OptimizationResultRecord
        from sqlalchemy import desc

        with get_session() as session:
            row = (
                session.query(OptimizationResultRecord)
                .filter_by(execution_id=execution_id)
                .order_by(desc(OptimizationResultRecord.profit_factor))
                .first()
            )
            if row and row.profit_factor is not None and row.profit_factor > self.best_profit_factor:
                self.best_profit_factor = float(row.profit_factor)
                try:
                    self.best_configuration = json.loads(row.parameters_json)
                except json.JSONDecodeError:
                    self.best_configuration = {"raw": row.parameters_json}
                self.best_asset = row.symbol
                self.best_timeframe = row.timeframe

    def _progress(self, current: int, total: int, pct: int, symbol: str, timeframe: str) -> None:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        elapsed = self.total_time_for("main_pipeline")
        est_total = elapsed / max(0.0001, current / total) if current else 0
        remaining = max(0.0, est_total - elapsed)
        self.night_logger.info(
            "[%d/%d] %s %s | %d%% | ETA %.1f min | CPU %.1f%% | RAM %.1f%%",
            current,
            total,
            symbol,
            timeframe,
            pct,
            remaining / 60,
            cpu,
            ram,
        )

    def total_time_for(self, step_name: str) -> float:
        return sum(step.duration_seconds for step in self.step_results if step.name == step_name)

    def _load_state(self) -> dict[str, Any]:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_checkpoint(self, stage: str, processed: int, completed: bool, payload: dict[str, Any] | None = None) -> None:
        try:
            execution_id = None
            if payload:
                execution_id = payload.get("execution_id")
            if execution_id is None:
                symbol, timeframe = self._stage_to_target(stage)
                execution_id = self._latest_optimization_execution_id(symbol, timeframe)
            if execution_id is None:
                execution_id = HistoryPersistenceService.new_execution_id()
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.save_checkpoint(
                    execution_id=execution_id,
                    stage=stage,
                    processed=processed,
                    completed=completed,
                    payload=payload,
                )
            self.progress.touch_checkpoint(stage, processed)
            self.progress.touch_heartbeat(stage=stage, combo=(payload or {}).get("label"), state="completed" if completed else "running")
            self._emit_timeline_event(
                "checkpoint_saved",
                "Checkpoint salvo",
                f"stage={stage} processed={processed} completed={completed}",
            )
        except Exception as exc:
            self.error_logger.exception("Checkpoint persistence failed for stage=%s: %s", stage, exc)

    def _stage_to_target(self, stage: str) -> tuple[str, str]:
        if stage == "smoke":
            return "BTC/USDT", "5m"
        if "_" in stage:
            symbol, timeframe = stage.split("_", 1)
            return symbol.replace("-", "/"), timeframe
        return settings.trading.default_symbol, settings.trading.default_timeframe

    def _write_heartbeat(self, stage: str | None = None, combo: str | None = None, state: str | None = None, final: bool = False) -> None:
        snapshot = self.progress.snapshot()
        if stage is not None:
            snapshot["stage"] = stage
        if combo is not None:
            snapshot["combo"] = combo
        if state is not None:
            snapshot["state"] = state
        snapshot["execution_id"] = self._current_execution_id()
        snapshot["pid"] = os.getpid()
        snapshot["cpu"] = psutil.cpu_percent(interval=None)
        snapshot["ram"] = psutil.virtual_memory().percent
        snapshot["final"] = final
        HEARTBEAT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def _current_execution_id(self) -> str | None:
        for step in reversed(self.step_results):
            execution_id = step.details.get("execution_id") if isinstance(step.details, dict) else None
            if execution_id:
                return str(execution_id)
        return None

    def _record_incident(self, stage: str, exc: Exception) -> None:
        self.incident = {
            "incident_type": stage,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "pid": os.getpid(),
            "execution_id": self._current_execution_id(),
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "traceback": traceback.format_exc(),
            "last_checkpoint": self.progress.last_checkpoint_at.isoformat() if self.progress.last_checkpoint_at else None,
            "last_log": self.progress.last_log_at.isoformat() if self.progress.last_log_at else None,
            "last_heartbeat": self.progress.last_heartbeat_at.isoformat() if self.progress.last_heartbeat_at else None,
            "last_db_update": self.progress.last_db_update_at.isoformat() if self.progress.last_db_update_at else None,
            "last_stage": self.progress.last_stage,
            "last_combo": self.progress.last_combo,
        }

    def _write_incident_report(self) -> None:
        lines = [
            "Night Runner Incident Report",
            "=" * 60,
            f"Execution ID: {self._current_execution_id() or ''}",
            f"PID: {os.getpid()}",
            f"Started: {self.progress.started_at.isoformat()}",
            f"Interrupted: {datetime.now(tz=timezone.utc).isoformat()}",
            f"Last checkpoint: {self.progress.last_checkpoint_at.isoformat() if self.progress.last_checkpoint_at else ''}",
            f"Last combination: {self.progress.last_combo or ''}",
            f"Quantity persisted: {self.progress.last_processed}",
            f"Quantity in memory: {self.total_combinations}",
            f"Last log: {self.progress.last_log_at.isoformat() if self.progress.last_log_at else ''}",
            f"Last heartbeat: {self.progress.last_heartbeat_at.isoformat() if self.progress.last_heartbeat_at else ''}",
            f"Last database update: {self.progress.last_db_update_at.isoformat() if self.progress.last_db_update_at else ''}",
            f"Total stalled seconds: {self.progress.stalled_seconds():.2f}",
            f"State: {self.progress.state}",
        ]
        if self.incident:
            lines.extend(["", json.dumps(self.incident, ensure_ascii=False, indent=2)])
        INCIDENT_FILE.write_text("\n".join(lines), encoding="utf-8")

    def _emit_timeline_event(self, event_type: str, title: str, details: str | None = None) -> None:
        try:
            with get_session() as session:
                session.add(
                    ExecutionTimelineEvent(
                        event_type=event_type,
                        title=title,
                        details=details,
                    )
                )
                session.flush()
        except Exception as exc:
            self.error_logger.exception("Timeline event persistence failed type=%s: %s", event_type, exc)

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()

        def _beat() -> None:
            while not self._heartbeat_stop.wait(60):
                self._write_heartbeat(state=self.progress.state or "running")

        self._heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)

    def _start_watchdog(self) -> None:
        self._watchdog_stop.clear()

        def _watch() -> None:
            threshold_seconds = int(os.getenv("NIGHT_RUNNER_STALL_SECONDS", str(15 * 60)))
            while not self._watchdog_stop.wait(60):
                stalled = self.progress.stalled_seconds()
                if stalled >= threshold_seconds:
                    self.error_logger.error("Watchdog detected stall for %.2fs", stalled)
                    self._record_incident("watchdog_stall", RuntimeError(f"Night runner stalled for {stalled:.2f}s"))
                    self.progress.state = "blocked"
                    self._abort_execution.set()
                    self._write_heartbeat(state="blocked")
                    self._write_incident_report()
                    self._emit_timeline_event("stall_detected", "Travamento detectado", f"stalled_seconds={stalled:.2f}")
                    break

        self._watchdog_thread = threading.Thread(target=_watch, daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Night runner automation")
    parser.add_argument("--dry-run", action="store_true", help="Run checks without executing pipeline")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = NightRunner(dry_run=args.dry_run)
    if args.dry_run:
        result = runner._step("preflight", runner._preflight_checks)
        return 0 if result.ok else 1
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
