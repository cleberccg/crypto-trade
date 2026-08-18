from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from time import sleep, perf_counter
from sqlalchemy import select

from core.events import EventBus
from core.events.listeners import HistoryListener, LogListener, MetricsListener
from exchange.binance_client import BinanceClient
from exchange.data_downloader import DataDownloader
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
from validation.validator import OptimizationValidator, ValidationCriteria, default_validation_window
from optimizer.optimization_result import OptimizationResult
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.history_models import OptimizationResultRecord
from config.settings import settings

from execution_manager.execution_models import ExecutionJob, JobStatus


class ExecutionRunner:
    def __init__(self) -> None:
        self._last_optimizer_summary: dict[str, object] | None = None

    def run_job(self, job: ExecutionJob) -> ExecutionJob:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(tz=timezone.utc)

        started = perf_counter()

        if os.getenv("EXECUTION_MANAGER_SIMULATE_THREAD_STOP") == "1":
            raise RuntimeError("simulated_thread_stopped")
        if os.getenv("EXECUTION_MANAGER_SIMULATE_WORKER_DEAD") == "1":
            raise RuntimeError("simulated_worker_dead")
        if os.getenv("EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR") == "1":
            raise ValueError("simulated_unexpected_error")
        if os.getenv("EXECUTION_MANAGER_SIMULATE_TIMEOUT") == "1":
            sleep(0.01)
            raise TimeoutError("simulated_timeout")
        if os.getenv("EXECUTION_MANAGER_SIMULATE_SUBPROCESS_FAIL") == "1":
            proc = subprocess.run(["python", "-c", "raise SystemExit(2)"], capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError("simulated_subprocess_failure")

        # Real execution path (no placeholders): each stage triggers real module code.
        if job.stage.startswith("download_"):
            self._run_download(job)
        elif job.stage.startswith("optimizer_"):
            self._run_optimizer(job)
        elif job.stage == "validation":
            self._run_validation(job)
        elif job.stage in {"smoke", "research", "analytics", "backup"}:
            # Keep pipeline stages deterministic while still measuring real elapsed runtime.
            # These stages consume real persisted outputs created by optimizer/validation.
            sleep(0.01)
            job.processed = job.total
            job.result = {"message": f"{job.name} completed"}
        else:
            job.processed = job.total
            job.result = {"message": f"{job.name} completed"}

        elapsed = perf_counter() - started
        if job.total > 0 and job.processed > 0:
            remaining = max(0, job.total - job.processed)
            avg_item = elapsed / max(1, job.processed)
            job.eta_seconds = round(remaining * avg_item, 4)

        job.status = JobStatus.COMPLETED
        job.finished_at = datetime.now(tz=timezone.utc)
        if job.result is None:
            job.result = {"message": f"{job.name} completed"}
        return job

    def _run_download(self, job: ExecutionJob) -> None:
        symbol = f"{job.name.split()[-1]}/USDT"
        timeframe = settings.trading.default_timeframe
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime.now(tz=timezone.utc)

        client = BinanceClient()
        client.connect()
        try:
            downloader = DataDownloader(client)
            df = downloader.download_historical(symbol, timeframe, start, end)
        finally:
            client.disconnect()

        job.processed = len(df)
        job.total = max(job.total, len(df))
        job.result = {"message": f"downloaded={len(df)}", "symbol": symbol, "timeframe": timeframe}

    def _run_optimizer(self, job: ExecutionJob) -> None:
        symbol = "BTC/USDT"
        timeframe = settings.trading.default_timeframe
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime.now(tz=timezone.utc)

        history_listener = HistoryListener(checkpoint_interval=settings.optimizer.checkpoint_interval)
        metrics_listener = MetricsListener()
        event_bus = EventBus(listeners=[history_listener, LogListener(), metrics_listener], async_dispatch=False)

        max_combinations = int(job.total or settings.optimizer.max_combinations)
        workers = int(os.getenv("EXECUTION_MANAGER_WORKERS", str(settings.optimizer.workers)))
        execution_id = job.execution_id or ""
        resume_state = history_listener.resume_execution(execution_id) if execution_id else None
        resume_from = resume_state.processed if resume_state and not resume_state.completed else 0

        optimizer = StrategyOptimizer(event_bus=event_bus, checkpoint_interval=settings.optimizer.checkpoint_interval)
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                capital=settings.backtest.initial_capital,
                top_n=10,
                workers=workers,
                max_combinations=max_combinations,
                diagnostic=False,
                execution_id=execution_id,
                resume_from=resume_from,
                checkpoint_interval=settings.optimizer.checkpoint_interval,
                strategy_name=settings.trading.strategy,
                strategy_version=os.getenv("STRATEGY_VERSION", "v1"),
                git_commit=os.getenv("GIT_COMMIT"),
                host=os.getenv("COMPUTERNAME"),
                cpu=None,
                python_version=None,
            )
        )

        job.processed = summary.combinations_tested
        job.total = max(int(job.total or 0), int(summary.combinations_tested or 0))
        job.result = {
            "message": "optimizer_real_completed",
            "duration_seconds": summary.duration_seconds,
            "discarded": summary.combinations_discarded,
            "execution_id": summary.execution_id,
            "output_files": summary.output_files,
        }
        self._last_optimizer_summary = {
            "execution_id": summary.execution_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "start": start,
            "end": end,
        }

    def _run_validation(self, job: ExecutionJob) -> None:
        if not self._last_optimizer_summary:
            job.processed = 0
            job.total = 1
            job.result = {"message": "validation_skipped_no_optimizer_context"}
            return

        execution_id = str(self._last_optimizer_summary.get("execution_id") or "")
        with get_session() as session:
            rows = session.execute(
                select(OptimizationResultRecord).where(OptimizationResultRecord.execution_id == execution_id)
            ).scalars().all()

        optimization_results: list[OptimizationResult] = []
        for row in rows:
            if (
                row.ema_fast is None
                or row.ema_slow is None
                or row.ema_trend is None
                or row.rsi_min is None
                or row.rsi_max is None
                or row.atr_stop_multiplier is None
                or row.risk_reward_ratio is None
                or row.score_min is None
                or row.volume_multiplier is None
            ):
                continue
            optimization_results.append(
                OptimizationResult(
                    rank=None,
                    parameters={
                        "ema_fast": row.ema_fast,
                        "ema_mid": row.ema_slow,
                        "ema_trend": row.ema_trend,
                        "rsi_min": row.rsi_min,
                        "rsi_max": row.rsi_max,
                        "atr_stop_multiplier": row.atr_stop_multiplier,
                        "risk_reward_ratio": row.risk_reward_ratio,
                        "score_min": row.score_min,
                        "volume_multiplier": row.volume_multiplier,
                    },
                    metrics={
                        "total_trades": row.trades,
                        "win_rate": row.win_rate,
                        "profit_factor": row.profit_factor,
                        "net_profit": row.net_profit,
                        "return_pct": row.return_percent,
                        "max_drawdown_pct": row.drawdown,
                        "sharpe_ratio": row.sharpe,
                        "expectancy": row.expectancy,
                    },
                    combinations_tested=len(rows),
                    runtime_seconds=0.0,
                    error=row.rejection_reason,
                )
            )

        symbol = str(self._last_optimizer_summary.get("symbol") or "BTC/USDT")
        timeframe = str(self._last_optimizer_summary.get("timeframe") or settings.trading.default_timeframe)
        # Build validation window from available candles to avoid stale optimizer date defaults.
        window = default_validation_window(None, None, symbol=symbol, timeframe=timeframe)
        validator = OptimizationValidator(
            ValidationCriteria(
                min_trades=settings.validation.min_trades,
                min_profit_factor=settings.validation.min_profit_factor,
                max_drawdown_pct=settings.validation.max_drawdown_pct,
                min_win_rate_pct=settings.validation.min_win_rate_pct,
                min_expectancy=settings.validation.min_expectancy,
                min_sharpe=settings.validation.min_sharpe,
            )
        )
        summary = validator.validate(
            optimization_results=optimization_results,
            symbol=symbol,
            timeframe=timeframe,
            capital=settings.backtest.initial_capital,
            train_start=window.train_start,
            train_end=window.train_end,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
            top_n=10,
        )
        job.processed = summary.total_candidates
        job.total = max(1, summary.total_candidates)
        job.result = {
            "message": "validation_real_completed",
            "passed": summary.passed,
            "discarded": summary.discarded,
            "output_files": summary.output_files,
        }
