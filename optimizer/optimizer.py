"""Independent strategy optimizer that runs many backtests in isolation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4
import json
import math
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from config.settings import settings
from core.events import EventBus, EventType, OptimizationEvent
from database.connection import get_session
from database.repositories import CandleRepository
from logging_service.logger_manager import bind_worker_logging_queue, get_logging_queue_handle
from optimizer.optimization_report import OptimizationReport
from optimizer.optimization_result import OptimizationResult
from optimizer.parameter_grid import ParameterGrid
from strategies.factory import create_strategy
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


@dataclass(frozen=True)
class OptimizerRunConfig:
    """Execution settings for a single optimization session."""

    symbol: str
    timeframe: str
    start: datetime
    end: datetime | None
    capital: float
    top_n: int
    workers: int
    max_combinations: int
    diagnostic: bool = False
    execution_id: str | None = None
    resume_from: int = 0
    checkpoint_interval: int = 50
    strategy_name: str = "TrendV1"
    strategy_version: str = "v1"
    git_commit: str | None = None
    host: str | None = None
    cpu: str | None = None
    python_version: str | None = None


@dataclass(frozen=True)
class OptimizationSummary:
    """High level summary for reporting."""

    combinations_tested: int
    combinations_discarded: int
    duration_seconds: float
    best_profit_factor: OptimizationResult | None
    best_net_profit: OptimizationResult | None
    lowest_drawdown: OptimizationResult | None
    best_sharpe: OptimizationResult | None
    top_results: list[OptimizationResult]
    output_files: list[str]
    execution_id: str


@dataclass(frozen=True)
class _EvaluationPayload:
    parameters: dict[str, Any]
    metrics: dict[str, float | int]
    error: str | None


_WORKER_DF: pd.DataFrame | None = None
_WORKER_SYMBOL: str | None = None
_WORKER_CAPITAL: float = 10_000.0
_WORKER_DIAGNOSTIC: bool = False
_WORKER_STRATEGY_NAME: str = "TrendV1"


def _init_worker(df: pd.DataFrame, symbol: str, capital: float, diagnostic: bool, strategy_name: str) -> None:
    global _WORKER_DF, _WORKER_SYMBOL, _WORKER_CAPITAL, _WORKER_DIAGNOSTIC, _WORKER_STRATEGY_NAME
    _WORKER_DF = df
    _WORKER_SYMBOL = symbol
    _WORKER_CAPITAL = capital
    _WORKER_DIAGNOSTIC = diagnostic
    _WORKER_STRATEGY_NAME = strategy_name


def _init_worker_with_logging(df: pd.DataFrame, symbol: str, capital: float, diagnostic: bool, strategy_name: str, queue) -> None:
    bind_worker_logging_queue(queue=queue, level=settings.logging.level)
    _init_worker(df=df, symbol=symbol, capital=capital, diagnostic=diagnostic, strategy_name=strategy_name)


def _safe_number(value: Any) -> float | int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isfinite(numeric) and numeric.is_integer():
        return int(numeric)
    return numeric


def _evaluate_parameters(parameters: dict[str, Any]) -> _EvaluationPayload:
    if _WORKER_DF is None or _WORKER_SYMBOL is None:
        raise RuntimeError("Optimizer worker not initialized.")

    try:
        strategy_params = dict(parameters)
        strategy_params.setdefault("rsi_period", 14)
        strategy_params.setdefault("atr_period", 14)

        strategy = create_strategy(_WORKER_STRATEGY_NAME, **strategy_params)
        strategy.initialize()

        # Prepare the full dataset once so the engine reuses precomputed features.
        strategy.prepare_dataset(_WORKER_DF.copy(), symbol=_WORKER_SYMBOL)
        engine = BacktestEngine(
            strategy,
            config=BacktestConfig(initial_capital=_WORKER_CAPITAL),
        )
        result = engine.run(_WORKER_DF, symbol=_WORKER_SYMBOL)

        metrics = result.metrics.to_dict()
        metrics.update(
            {
                "buy_signals": sum(1 for trade in result.trades if trade.get("entry_price") is not None),
                "sell_signals": sum(1 for trade in result.trades if trade.get("exit_price") is not None),
                "return_pct_raw": result.metrics.return_pct,
            }
        )
        if "ema_trend" in parameters:
            metrics["ema_trend"] = _safe_number(parameters["ema_trend"])
        return _EvaluationPayload(parameters=parameters, metrics=metrics, error=None)
    except KeyboardInterrupt:
        # Absorb the signal so the optimizer loop can continue with remaining combinations
        return _EvaluationPayload(parameters=parameters, metrics={}, error="KeyboardInterrupt")
    except Exception as exc:  # pragma: no cover - defensive guard
        return _EvaluationPayload(parameters=parameters, metrics={}, error=str(exc))


class StrategyOptimizer:
    """Run large parameter sweeps over TrendV1 using isolated backtests."""

    def __init__(
        self,
        grid: ParameterGrid | None = None,
        output_dir: Path | None = None,
        event_bus: EventBus | None = None,
        checkpoint_interval: int = 50,
    ) -> None:
        self._grid = grid or ParameterGrid()
        self._reporter = OptimizationReport(output_dir=output_dir)
        self._event_bus = event_bus or EventBus()
        self._checkpoint_interval = max(1, checkpoint_interval)

    def _publish(self, event_type: EventType, execution_id: str, payload: dict[str, Any]) -> None:
        self._event_bus.publish(
            OptimizationEvent(
                event_type=event_type,
                execution_id=execution_id,
                payload=payload,
            )
        )

    def resume_execution(self, execution_id: str) -> str:
        """Return execution id for external listener-driven resume logic."""
        return execution_id

    def _load_dataframe(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None,
    ) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end or datetime.now(tz=timezone.utc))

        if not candles:
            raise ValueError(f"No historical data found for {symbol}/{timeframe}.")

        return pd.DataFrame(
            [
                {
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            index=pd.DatetimeIndex([candle.open_time for candle in candles], tz="UTC"),
        )

    def _rank_results(self, results: list[OptimizationResult]) -> list[OptimizationResult]:
        def sort_key(result: OptimizationResult) -> tuple[float, float, float, float]:
            metrics = result.metrics
            return (
                -float(metrics.get("profit_factor", 0.0)),
                -float(metrics.get("net_profit", 0.0)),
                float(metrics.get("max_drawdown_pct", 0.0)),
                -float(metrics.get("sharpe_ratio", 0.0)),
            )

        ranked = sorted(results, key=sort_key)
        final: list[OptimizationResult] = []
        for index, result in enumerate(ranked, start=1):
            final.append(
                OptimizationResult(
                    rank=index,
                    parameters=result.parameters,
                    metrics=result.metrics,
                    combinations_tested=result.combinations_tested,
                    runtime_seconds=result.runtime_seconds,
                    error=result.error,
                )
            )
        return final

    def run(self, config: OptimizerRunConfig) -> OptimizationSummary:
        execution_id = config.execution_id or str(uuid4())
        self._checkpoint_interval = max(1, config.checkpoint_interval)
        start_time = perf_counter()
        df = self._load_dataframe(config.symbol, config.timeframe, config.start, config.end)

        combinations = list(
            islice(
                self._grid.combinations(limit=config.max_combinations, strategy_name=config.strategy_name),
                config.max_combinations,
            )
        )
        if config.resume_from > 0:
            combinations = combinations[config.resume_from :]
        evaluated: list[OptimizationResult] = []
        discarded = 0

        logger.info(
            "Optimizer starting - symbol=%s timeframe=%s combinations=%d workers=%d",
            config.symbol,
            config.timeframe,
            len(combinations),
            config.workers,
        )

        self._publish(
            EventType.OPTIMIZER_STARTED,
            execution_id,
            {
                "started_at": datetime.now(tz=timezone.utc),
                "strategy": config.strategy_name,
                "strategy_version": config.strategy_version,
                "git_commit": config.git_commit,
                "symbol": config.symbol,
                "timeframe": config.timeframe,
                "total_combinations": len(combinations),
                "workers": config.workers,
                "host": config.host,
                "cpu": config.cpu,
                "python_version": config.python_version,
                "resume_from": config.resume_from,
            },
        )

        if config.workers > 1:
            shared_queue = get_logging_queue_handle()
            with ProcessPoolExecutor(
                max_workers=config.workers,
                initializer=_init_worker_with_logging,
                initargs=(df, config.symbol, config.capital, config.diagnostic, config.strategy_name, shared_queue),
            ) as executor:
                futures = {executor.submit(_evaluate_parameters, params): params for params in combinations}
                pending = set(futures.keys())
                last_progress_at = perf_counter()
                last_processed = 0
                last_params: dict[str, Any] | None = None
                first_result_elapsed: float | None = None

                while pending:
                    try:
                        future = next(as_completed(pending, timeout=60))
                    except FuturesTimeoutError:
                        cpu_pct = psutil.cpu_percent(interval=None) if psutil else -1.0
                        mem_pct = float(psutil.virtual_memory().percent) if psutil else -1.0
                        processed_now = len(evaluated) + discarded
                        bottleneck_hint = " | suspected_bottleneck=full_recalculation_or_heavy_backtest" if processed_now == 0 and (perf_counter() - last_progress_at) >= 300 else ""
                        logger.warning(
                            "Sem progresso detectado ha %.0f segundos. "
                            "ultimo_processado=%d/%d ultimo_params=%s ultimo_checkpoint=%d "
                            "cpu=%.1f%% mem=%.1f%% pendentes=%d%s",
                            perf_counter() - last_progress_at,
                            processed_now,
                            len(combinations),
                            last_params,
                            len(evaluated),
                            cpu_pct,
                            mem_pct,
                            len(pending),
                            bottleneck_hint,
                        )
                        continue

                    pending.remove(future)
                    params = futures[future]
                    last_params = params
                    self._publish(
                        EventType.COMBINATION_STARTED,
                        execution_id,
                        {
                            "strategy": config.strategy_name,
                            "symbol": config.symbol,
                            "timeframe": config.timeframe,
                            "parameters": params,
                        },
                    )
                    payload = future.result()
                    if first_result_elapsed is None:
                        first_result_elapsed = perf_counter() - start_time
                        logger.info(
                            "Optimizer first result - strategy=%s elapsed=%.2fs pending=%d",
                            config.strategy_name,
                            first_result_elapsed,
                            len(pending),
                        )
                    if payload.error:
                        discarded += 1
                        result_obj = OptimizationResult(
                            rank=None,
                            parameters=payload.parameters,
                            metrics={},
                            combinations_tested=len(combinations),
                            runtime_seconds=0.0,
                            error=payload.error,
                        )
                        evaluated.append(result_obj)
                        self._publish(
                            EventType.COMBINATION_FINISHED,
                            execution_id,
                            {
                                "strategy": config.strategy_name,
                                "symbol": config.symbol,
                                "timeframe": config.timeframe,
                                "result": result_obj.to_dict(),
                            },
                        )
                        _opt_n = len(evaluated) + discarded
                        _opt_el = perf_counter() - start_time
                        _opt_rate = _opt_n / _opt_el * 60 if _opt_el > 0 else 0.0
                        _opt_eta = (len(combinations) - _opt_n) / (_opt_n / _opt_el) if _opt_n > 0 and _opt_el > 0 else 0.0
                        logger.info(
                            "Optimizer — %d/%d (%.1f%%) | %.1f comb/min | ETA %.0fs | discarded=%d",
                            _opt_n, len(combinations), _opt_n / len(combinations) * 100,
                            _opt_rate, _opt_eta, discarded,
                        )
                        if _opt_n != last_processed:
                            last_processed = _opt_n
                            last_progress_at = perf_counter()
                        continue
                    result_obj = OptimizationResult(
                        rank=None,
                        parameters=payload.parameters,
                        metrics=payload.metrics,
                        combinations_tested=len(combinations),
                        runtime_seconds=0.0,
                        error=None,
                    )
                    evaluated.append(result_obj)
                    self._publish(
                        EventType.COMBINATION_FINISHED,
                        execution_id,
                        {
                            "strategy": config.strategy_name,
                            "symbol": config.symbol,
                            "timeframe": config.timeframe,
                            "result": result_obj.to_dict(),
                        },
                    )
                    self._publish(
                        EventType.COMBINATION_SAVED,
                        execution_id,
                        {
                            "processed": len(evaluated),
                            "last_rank": result_obj.rank,
                            "parameters": result_obj.parameters,
                        },
                    )
                    if len(evaluated) % self._checkpoint_interval == 0:
                        self._publish(
                            EventType.CHECKPOINT,
                            execution_id,
                            {
                                "stage": "optimizer",
                                "processed": len(evaluated),
                                "completed": False,
                                "parameters": result_obj.parameters,
                                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                            },
                        )
                    _opt_n = len(evaluated) + discarded
                    _opt_el = perf_counter() - start_time
                    _opt_rate = _opt_n / _opt_el * 60 if _opt_el > 0 else 0.0
                    _opt_eta = (len(combinations) - _opt_n) / (_opt_n / _opt_el) if _opt_n > 0 and _opt_el > 0 else 0.0
                    _opt_best = max(
                        (float(r.metrics.get("profit_factor", 0.0)) for r in evaluated if r.metrics and not r.error),
                        default=0.0,
                    )
                    logger.info(
                        "Optimizer — %d/%d (%.1f%%) | %.1f comb/min | ETA %.0fs | best_pf=%.4f | discarded=%d",
                        _opt_n, len(combinations), _opt_n / len(combinations) * 100,
                        _opt_rate, _opt_eta, _opt_best, discarded,
                    )
                    if _opt_n != last_processed:
                        last_processed = _opt_n
                        last_progress_at = perf_counter()
        else:
            _init_worker(df, config.symbol, config.capital, config.diagnostic, config.strategy_name)
            for params in combinations:
                self._publish(
                    EventType.COMBINATION_STARTED,
                    execution_id,
                    {
                        "strategy": config.strategy_name,
                        "symbol": config.symbol,
                        "timeframe": config.timeframe,
                        "parameters": params,
                    },
                )
                payload = _evaluate_parameters(params)
                if len(evaluated) + discarded == 0:
                    logger.info(
                        "Optimizer first result - strategy=%s elapsed=%.2fs",
                        config.strategy_name,
                        perf_counter() - start_time,
                    )
                if payload.error:
                    discarded += 1
                    result_obj = OptimizationResult(
                        rank=None,
                        parameters=payload.parameters,
                        metrics={},
                        combinations_tested=len(combinations),
                        runtime_seconds=0.0,
                        error=payload.error,
                    )
                    evaluated.append(result_obj)
                    self._publish(
                        EventType.COMBINATION_FINISHED,
                        execution_id,
                        {
                            "strategy": config.strategy_name,
                            "symbol": config.symbol,
                            "timeframe": config.timeframe,
                            "result": result_obj.to_dict(),
                        },
                    )
                    _opt_n = len(evaluated) + discarded
                    _opt_el = perf_counter() - start_time
                    _opt_rate = _opt_n / _opt_el * 60 if _opt_el > 0 else 0.0
                    _opt_eta = (len(combinations) - _opt_n) / (_opt_n / _opt_el) if _opt_n > 0 and _opt_el > 0 else 0.0
                    logger.info(
                        "Optimizer — %d/%d (%.1f%%) | %.1f comb/min | ETA %.0fs | discarded=%d",
                        _opt_n, len(combinations), _opt_n / len(combinations) * 100,
                        _opt_rate, _opt_eta, discarded,
                    )
                    continue
                result_obj = OptimizationResult(
                    rank=None,
                    parameters=payload.parameters,
                    metrics=payload.metrics,
                    combinations_tested=len(combinations),
                    runtime_seconds=0.0,
                    error=None,
                )
                evaluated.append(result_obj)
                self._publish(
                    EventType.COMBINATION_FINISHED,
                    execution_id,
                    {
                        "strategy": config.strategy_name,
                        "symbol": config.symbol,
                        "timeframe": config.timeframe,
                        "result": result_obj.to_dict(),
                    },
                )
                self._publish(
                    EventType.COMBINATION_SAVED,
                    execution_id,
                    {
                        "processed": len(evaluated),
                        "last_rank": result_obj.rank,
                        "parameters": result_obj.parameters,
                    },
                )
                if len(evaluated) % self._checkpoint_interval == 0:
                    self._publish(
                        EventType.CHECKPOINT,
                        execution_id,
                        {
                            "stage": "optimizer",
                            "processed": len(evaluated),
                            "completed": False,
                            "parameters": result_obj.parameters,
                            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                        },
                    )
                _opt_n = len(evaluated) + discarded
                _opt_el = perf_counter() - start_time
                _opt_rate = _opt_n / _opt_el * 60 if _opt_el > 0 else 0.0
                _opt_eta = (len(combinations) - _opt_n) / (_opt_n / _opt_el) if _opt_n > 0 and _opt_el > 0 else 0.0
                _opt_best = max(
                    (float(r.metrics.get("profit_factor", 0.0)) for r in evaluated if r.metrics and not r.error),
                    default=0.0,
                )
                logger.info(
                    "Optimizer — %d/%d (%.1f%%) | %.1f comb/min | ETA %.0fs | best_pf=%.4f | discarded=%d",
                    _opt_n, len(combinations), _opt_n / len(combinations) * 100,
                    _opt_rate, _opt_eta, _opt_best, discarded,
                )

        ranked = self._rank_results([result for result in evaluated if result.error is None])
        full_ranked = ranked
        ranked = ranked[: config.top_n]
        duration = perf_counter() - start_time

        for result in full_ranked:
            result.runtime_seconds = duration

        output_files = []
        output_files.append(str(self._reporter.save_csv(full_ranked)))
        output_files.append(str(self._reporter.save_json(full_ranked)))
        summary_text = self._render_summary(
            combinations_tested=len(combinations),
            combinations_discarded=discarded,
            results=full_ranked,
            duration_seconds=duration,
        )
        output_files.append(str(self._reporter.save_text_report(summary_text)))

        sqlite_path = self._reporter.output_dir / "optimization_results.db"
        self._save_sqlite(sqlite_path, full_ranked)
        output_files.append(str(sqlite_path))

        best_pf = full_ranked[0] if full_ranked else None
        best_profit = max(full_ranked, key=lambda item: float(item.metrics.get("net_profit", 0.0)), default=None)
        lowest_dd = min(full_ranked, key=lambda item: float(item.metrics.get("max_drawdown_pct", 0.0)), default=None)
        best_sharpe = max(full_ranked, key=lambda item: float(item.metrics.get("sharpe_ratio", 0.0)), default=None)

        self._publish(
            EventType.OPTIMIZER_FINISHED,
            execution_id,
            {
                "finished_at": datetime.now(tz=timezone.utc),
                "duration_seconds": duration,
                "processed": len(evaluated),
                "status": "completed",
            },
        )

        return OptimizationSummary(
            combinations_tested=len(combinations),
            combinations_discarded=discarded,
            duration_seconds=duration,
            best_profit_factor=best_pf,
            best_net_profit=best_profit,
            lowest_drawdown=lowest_dd,
            best_sharpe=best_sharpe,
            top_results=ranked,
            output_files=output_files,
            execution_id=execution_id,
        )

    def _save_sqlite(self, sqlite_path: Path, results: list[OptimizationResult]) -> None:
        if sqlite_path.exists():
            sqlite_path.unlink()

        with sqlite3.connect(sqlite_path) as connection:
            connection.execute(
                """
                CREATE TABLE optimization_results (
                    rank INTEGER,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    combinations_tested INTEGER NOT NULL,
                    runtime_seconds REAL NOT NULL,
                    error TEXT
                )
                """
            )
            for result in results:
                connection.execute(
                    "INSERT INTO optimization_results VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        result.rank,
                        json.dumps(result.parameters, ensure_ascii=False),
                        json.dumps(result.metrics, ensure_ascii=False),
                        result.combinations_tested,
                        result.runtime_seconds,
                        result.error,
                    ),
                )
            connection.commit()

    def _render_summary(
        self,
        combinations_tested: int,
        combinations_discarded: int,
        results: list[OptimizationResult],
        duration_seconds: float,
    ) -> str:
        lines = [
            "======================================",
            "OPTIMIZATION REPORT",
            "======================================",
            f"Tempo total da otimizacao: {duration_seconds:.2f}s",
            f"Combinacoes testadas: {combinations_tested}",
            f"Combinacoes descartadas: {combinations_discarded}",
            "",
            "Melhor Profit Factor:",
        ]
        if results:
            lines.append(self._format_result(results[0]))
        else:
            lines.append("Nenhum resultado valido.")
        lines.extend(
            [
                "",
                "TOP 10",
            ]
        )
        for result in results[:10]:
            lines.append(self._format_result(result))
        lines.extend(["", "Arquivos gerados:"])
        for filename in ["optimization_results.csv", "optimization_results.json", "optimization_results.db"]:
            lines.append(f"- {filename}")
        lines.append("======================================")
        return "\n".join(lines)

    def _format_result(self, result: OptimizationResult) -> str:
        params = result.parameters
        metrics = result.metrics
        params_repr = ", ".join(f"{key}={value}" for key, value in sorted(params.items()))
        return (
            f"Rank {result.rank} | Params {params_repr} | "
            f"PF {metrics.get('profit_factor', 0):.2f} | Win {metrics.get('win_rate', 0):.2%} | "
            f"DD {metrics.get('max_drawdown_pct', 0):.2%} | Lucro {metrics.get('return_pct', 0):+.2%}"
        )
