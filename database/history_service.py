"""High level persistence service for system history and checkpoints."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backtesting.engine import BacktestResult
from database.history_models import (
    BacktestRun,
    ExecutionCheckpoint,
    IndicatorHistorySnapshot,
    OptimizationRun,
    OptimizationResultRecord,
    ScientificReadinessHistory,
    ScientificTradeSnapshot,
    SignalSnapshot,
    TradeHistory,
    ValidationRun,
)
from database.history_repositories import (
    BacktestRunRepository,
    ExecutionSessionRepository,
    IndicatorSnapshotRepository,
    OptimizationResultRepository,
    OptimizationRunRepository,
    ScientificReadinessHistoryRepository,
    ScientificTradeSnapshotRepository,
    SignalHistoryRepository,
    StrategyVersionRepository,
    TradeHistoryRepository,
    ValidationRunRepository,
)
from optimizer.optimization_result import OptimizationResult
from optimizer.optimizer import OptimizationSummary
from validation.validator import ValidationSummary
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PersistenceBatchResult:
    inserted: int
    duration_seconds: float


class HistoryPersistenceService:
    """Persist execution history without requiring changes to core engines."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._optimization_runs = OptimizationRunRepository(session)
        self._optimization_results = OptimizationResultRepository(session)
        self._backtest_runs = BacktestRunRepository(session)
        self._trades = TradeHistoryRepository(session)
        self._signals = SignalHistoryRepository(session)
        self._indicator_snapshots = IndicatorSnapshotRepository(session)
        self._scientific_trade_snapshots = ScientificTradeSnapshotRepository(session)
        self._scientific_readiness_history = ScientificReadinessHistoryRepository(session)
        self._validation_runs = ValidationRunRepository(session)
        self._strategy_versions = StrategyVersionRepository(session)
        self._execution_sessions = ExecutionSessionRepository(session)

    @staticmethod
    def new_execution_id() -> str:
        return str(uuid.uuid4())

    def create_optimization_run(
        self,
        execution_id: str,
        started_at: datetime,
        strategy: str,
        symbol: str,
        timeframe: str,
        total_combinations: int,
        workers: int,
        status: str,
    ) -> OptimizationRun:
        run = OptimizationRun(
            execution_id=execution_id,
            started_at=started_at,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            total_combinations=total_combinations,
            workers=workers,
            status=status,
        )
        return self._optimization_runs.create(run)

    def finish_optimization_run(
        self,
        execution_id: str,
        finished_at: datetime,
        duration_seconds: float,
        status: str,
    ) -> OptimizationRun | None:
        return self._optimization_runs.update_status(
            execution_id=execution_id,
            status=status,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        )

    def register_strategy_version(
        self,
        strategy_name: str,
        version: str,
        git_commit: str | None,
        description: str | None = None,
    ) -> None:
        self._strategy_versions.get_or_create(
            strategy_name=strategy_name,
            version=version,
            git_commit=git_commit,
            description=description,
        )

    def start_execution_session(
        self,
        execution_id: str,
        started_at: datetime,
        status: str,
        host: str | None,
        cpu: str | None,
        workers: int | None,
        python_version: str | None,
        git_version: str | None,
    ) -> None:
        self._execution_sessions.create_or_update_started(
            execution_id=execution_id,
            started_at=started_at,
            status=status,
            host=host,
            cpu=cpu,
            workers=workers,
            python_version=python_version,
            git_version=git_version,
        )

    def finish_execution_session(
        self,
        execution_id: str,
        finished_at: datetime,
        duration: float,
        status: str,
    ) -> None:
        self._execution_sessions.finish(
            execution_id=execution_id,
            finished_at=finished_at,
            duration=duration,
            status=status,
        )

    def save_optimization_result(
        self,
        execution_id: str,
        symbol: str,
        timeframe: str,
        strategy: str,
        result: OptimizationResult,
        approved: bool,
        rejection_reason: str | None = None,
    ) -> OptimizationResultRecord:
        parameters = result.parameters
        record = OptimizationResultRecord(
            execution_id=execution_id,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            parameters_json=json.dumps(parameters, ensure_ascii=False),
            ema_fast=_as_int(parameters.get("ema_fast")),
            ema_slow=_as_int(parameters.get("ema_mid")),
            ema_trend=_as_int(parameters.get("ema_trend")),
            rsi_min=_as_float(parameters.get("rsi_min")),
            rsi_max=_as_float(parameters.get("rsi_max")),
            atr_stop_multiplier=_as_float(parameters.get("atr_stop_multiplier")),
            risk_reward_ratio=_as_float(parameters.get("risk_reward_ratio")),
            score_min=_as_float(parameters.get("score_min")),
            volume_multiplier=_as_float(parameters.get("volume_multiplier")),
            trades=_as_int(result.metrics.get("total_trades")),
            win_rate=_as_float(result.metrics.get("win_rate")),
            profit_factor=_as_float(result.metrics.get("profit_factor")),
            net_profit=_as_float(result.metrics.get("net_profit")),
            return_percent=_as_float(result.metrics.get("return_pct")),
            drawdown=_as_float(result.metrics.get("max_drawdown_pct")),
            sharpe=_as_float(result.metrics.get("sharpe_ratio")),
            expectancy=_as_float(result.metrics.get("expectancy")),
            approved=approved,
            rejection_reason=rejection_reason,
        )
        return self._optimization_results.save(record)

    def save_optimization_results_batch(
        self,
        execution_id: str,
        symbol: str,
        timeframe: str,
        strategy: str,
        results: list[OptimizationResult],
        approved_ids: set[int] | None = None,
    ) -> PersistenceBatchResult:
        start = perf_counter()
        inserted = 0
        approved_ids = approved_ids or set()
        for result in results:
            approved = result.rank in approved_ids if result.rank is not None else False
            self.save_optimization_result(
                execution_id=execution_id,
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                result=result,
                approved=approved,
                rejection_reason=None if approved else result.error or "rejected_by_validation",
            )
            inserted += 1
        duration = perf_counter() - start
        logger.info("Optimization results batch persisted: rows=%d duration=%.4fs", inserted, duration)
        return PersistenceBatchResult(inserted=inserted, duration_seconds=duration)

    def create_backtest_run(
        self,
        execution_id: str,
        strategy: str,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        initial_capital: float,
        status: str,
        final_capital: float | None = None,
        total_trades: int | None = None,
        win_rate: float | None = None,
        profit_factor: float | None = None,
        sharpe: float | None = None,
        expectancy: float | None = None,
        drawdown: float | None = None,
    ) -> BacktestRun:
        run = BacktestRun(
            execution_id=execution_id,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_capital=final_capital,
            total_trades=total_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe=sharpe,
            expectancy=expectancy,
            drawdown=drawdown,
            status=status,
        )
        return self._backtest_runs.save(run)

    def save_backtest_result(
        self,
        execution_id: str,
        result: BacktestResult,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        status: str = "completed",
    ) -> BacktestRun:
        metrics = result.metrics
        return self.create_backtest_run(
            execution_id=execution_id,
            strategy=result.strategy_name,
            symbol=result.symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=result.config.initial_capital,
            status=status,
            final_capital=metrics.final_capital,
            total_trades=metrics.total_trades,
            win_rate=metrics.win_rate,
            profit_factor=metrics.profit_factor,
            sharpe=metrics.sharpe_ratio,
            expectancy=metrics.expectancy,
            drawdown=abs(metrics.max_drawdown_pct),
        )

    def save_signals_from_dataframe(
        self,
        execution_id: str,
        strategy: str,
        symbol: str,
        timeframe: str,
        df: Any,
    ) -> tuple[int, int]:
        signal_rows: list[SignalSnapshot] = []
        indicator_rows: list[IndicatorHistorySnapshot] = []
        for timestamp, row in df.iterrows():
            signal_type = str(row.get("signal", "HOLD"))
            accepted = signal_type in {"BUY", "SELL"}
            signal = SignalSnapshot(
                execution_id=execution_id,
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                timestamp=timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp,
                signal=signal_type,
                score=_as_float(row.get("score")),
                entry_price=_as_float(row.get("entry_price")),
                stop_loss=_as_float(row.get("stop_loss")),
                take_profit=_as_float(row.get("take_profit")),
                rr=_as_float(row.get("rr")),
                accepted=accepted,
                rejection_reason=None if accepted else str(row.get("rejection_reason")) if row.get("rejection_reason") else None,
                market_regime=str(row.get("market_regime")) if row.get("market_regime") is not None else None,
            )
            signal_rows.append(signal)

        if signal_rows:
            self._signals.save_many(signal_rows)
            for signal, (_, row) in zip(signal_rows, df.iterrows()):
                if signal.signal in {"BUY", "SELL"}:
                    indicator_rows.append(
                        IndicatorHistorySnapshot(
                            signal=signal,
                            ema_fast=_as_float(row.get("ema_fast")),
                            ema_slow=_as_float(row.get("ema_slow")),
                            ema_trend=_as_float(row.get("ema_trend")),
                            rsi=_as_float(row.get("rsi")),
                            atr=_as_float(row.get("atr")),
                            volume=_as_float(row.get("volume")),
                            volume_average=_as_float(row.get("volume_average")),
                            close=_as_float(row.get("close")),
                            high=_as_float(row.get("high")),
                            low=_as_float(row.get("low")),
                        )
                    )
            if indicator_rows:
                self._indicator_snapshots.save_many(indicator_rows)

        return len(signal_rows), len(indicator_rows)

    def save_signal_snapshot(
        self,
        *,
        execution_id: str,
        strategy: str,
        symbol: str,
        timeframe: str,
        timestamp: datetime,
        signal: str,
        score: float | None,
        entry_price: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        rr: float | None,
        accepted: bool,
        rejection_reason: str | None,
        market_regime: str | None,
        indicator_payload: dict[str, Any] | None = None,
    ) -> SignalSnapshot:
        signal_row = SignalSnapshot(
            execution_id=execution_id,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            signal=signal,
            score=_as_float(score),
            entry_price=_as_float(entry_price),
            stop_loss=_as_float(stop_loss),
            take_profit=_as_float(take_profit),
            rr=_as_float(rr),
            accepted=accepted,
            rejection_reason=rejection_reason,
            market_regime=market_regime,
        )
        self._session.add(signal_row)
        self._session.flush()
        if signal in {"BUY", "SELL"} and indicator_payload is not None:
            self._session.add(
                IndicatorHistorySnapshot(
                    signal=signal_row,
                    ema_fast=_as_float(indicator_payload.get("ema_fast")),
                    ema_slow=_as_float(indicator_payload.get("ema_slow")),
                    ema_trend=_as_float(indicator_payload.get("ema_trend")),
                    rsi=_as_float(indicator_payload.get("rsi")),
                    atr=_as_float(indicator_payload.get("atr")),
                    volume=_as_float(indicator_payload.get("volume")),
                    volume_average=_as_float(indicator_payload.get("volume_average")),
                    close=_as_float(indicator_payload.get("close")),
                    high=_as_float(indicator_payload.get("high")),
                    low=_as_float(indicator_payload.get("low")),
                )
            )
        self._session.commit()
        return signal_row

    def save_trade_row(
        self,
        *,
        execution_id: str,
        strategy: str,
        symbol: str,
        timeframe: str,
        trade: dict[str, Any],
    ) -> TradeHistory:
        row = TradeHistory(
            execution_id=execution_id,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            side=str(trade.get("side", "BUY")),
            entry_time=_ensure_datetime(trade.get("entry_time")),
            exit_time=_ensure_datetime(trade.get("exit_time")),
            entry_price=_as_float(trade.get("entry_price")) or 0.0,
            exit_price=_as_float(trade.get("exit_price")),
            stop_loss=_as_float(trade.get("stop_loss")),
            take_profit=_as_float(trade.get("take_profit")),
            risk_reward=_as_float(trade.get("risk_reward")),
            quantity=_as_float(trade.get("quantity")) or 0.0,
            pnl=_as_float(trade.get("pnl")),
            pnl_percent=_as_float(trade.get("pnl_pct")),
            duration_minutes=_as_float(trade.get("duration_minutes")),
            exit_reason=str(trade.get("exit_reason")) if trade.get("exit_reason") is not None else None,
            score=_as_float(trade.get("score")),
        )
        self._session.add(row)
        self._session.commit()
        return row

    def save_scientific_trade_snapshot(self, snapshot: ScientificTradeSnapshot) -> ScientificTradeSnapshot:
        return self._scientific_trade_snapshots.save(snapshot)

    def update_scientific_trade_snapshot_exit(
        self,
        *,
        snapshot_id: int,
        trade_history_id: int | None,
        exit_snapshot: dict[str, Any],
        snapshot_complete: bool,
        missing_fields: list[str],
    ) -> ScientificTradeSnapshot | None:
        row = self._session.get(ScientificTradeSnapshot, snapshot_id)
        if row is None:
            return None
        row.trade_history_id = trade_history_id
        row.exit_snapshot_json = json.dumps(exit_snapshot, ensure_ascii=False)
        row.snapshot_complete = snapshot_complete
        row.missing_fields_json = json.dumps(missing_fields, ensure_ascii=False)
        self._session.commit()
        return row

    def save_scientific_readiness_history(self, record: ScientificReadinessHistory) -> ScientificReadinessHistory:
        return self._scientific_readiness_history.save(record)

    def save_trades_from_backtest(
        self,
        execution_id: str,
        strategy: str,
        symbol: str,
        timeframe: str,
        trades: list[dict[str, Any]],
    ) -> int:
        rows: list[TradeHistory] = []
        for trade in trades:
            rows.append(
                TradeHistory(
                    execution_id=execution_id,
                    strategy=strategy,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=str(trade.get("side", "BUY")),
                    entry_time=_ensure_datetime(trade.get("entry_time")),
                    exit_time=_ensure_datetime(trade.get("exit_time")),
                    entry_price=_as_float(trade.get("entry_price")) or 0.0,
                    exit_price=_as_float(trade.get("exit_price")),
                    stop_loss=_as_float(trade.get("stop_loss")),
                    take_profit=_as_float(trade.get("take_profit")),
                    risk_reward=_as_float(trade.get("risk_reward")),
                    quantity=_as_float(trade.get("quantity")) or 0.0,
                    pnl=_as_float(trade.get("pnl")),
                    pnl_percent=_as_float(trade.get("pnl_pct")),
                    duration_minutes=_as_float(trade.get("duration_minutes")),
                    exit_reason=str(trade.get("exit_reason")) if trade.get("exit_reason") is not None else None,
                    score=_as_float(trade.get("score")),
                )
            )
        if rows:
            self._trades.save_many(rows)
        return len(rows)

    def save_validation_run(
        self,
        execution_id: str,
        optimizer_run: str | None,
        total_tested: int,
        approved: int,
        rejected: int,
        min_profit_factor: float,
        min_trades: int,
        max_drawdown: float,
        validation_status: str,
    ) -> ValidationRun:
        run = ValidationRun(
            execution_id=execution_id,
            optimizer_run=optimizer_run,
            total_tested=total_tested,
            approved=approved,
            rejected=rejected,
            min_profit_factor=min_profit_factor,
            min_trades=min_trades,
            max_drawdown=max_drawdown,
            validation_status=validation_status,
        )
        return self._validation_runs.save(run)

    def save_checkpoint(
        self,
        execution_id: str,
        stage: str,
        processed: int,
        completed: bool,
        payload: dict[str, Any] | None = None,
    ) -> ExecutionCheckpoint:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                # Keepalive ping so long-running campaigns detect stale connections
                # before trying to persist the checkpoint row.
                self._session.execute(text("SELECT 1"))

                checkpoint = ExecutionCheckpoint(
                    execution_id=execution_id,
                    stage=stage,
                    processed=processed,
                    completed=completed,
                    payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                )
                self._session.add(checkpoint)
                self._session.flush()
                self._session.commit()
                logger.info(
                    "Checkpoint saved: execution_id=%s stage=%s processed=%d completed=%s",
                    execution_id,
                    stage,
                    processed,
                    completed,
                )
                return checkpoint
            except OperationalError as exc:
                self._session.rollback()
                last_error = exc
                logger.warning(
                    "Checkpoint persistence retry after OperationalError: execution_id=%s stage=%s attempt=%d error=%s",
                    execution_id,
                    stage,
                    attempt + 1,
                    exc,
                )

                # Retry with a fresh DB session to survive dropped MySQL connections.
                if attempt == 0:
                    from database.connection import get_session

                    with get_session() as fresh_session:
                        checkpoint = ExecutionCheckpoint(
                            execution_id=execution_id,
                            stage=stage,
                            processed=processed,
                            completed=completed,
                            payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                        )
                        fresh_session.add(checkpoint)
                        fresh_session.flush()
                        logger.info(
                            "Checkpoint saved with fresh session: execution_id=%s stage=%s processed=%d completed=%s",
                            execution_id,
                            stage,
                            processed,
                            completed,
                        )
                        return checkpoint

        if last_error is not None:
            raise last_error
        raise RuntimeError("checkpoint_persistence_failed_without_error")

    def get_latest_checkpoint(self, execution_id: str, stage: str) -> ExecutionCheckpoint | None:
        return (
            self._session.query(ExecutionCheckpoint)
            .filter_by(execution_id=execution_id, stage=stage)
            .order_by(ExecutionCheckpoint.created_at.desc(), ExecutionCheckpoint.id.desc())
            .first()
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        return datetime.now(tz=timezone.utc)
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return datetime.fromisoformat(str(value))
