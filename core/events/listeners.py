"""Default listeners for optimizer events (history, logs, metrics)."""
from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from core.events.events import EventType, OptimizationEvent
from core.events.interfaces import EventListener
from database.connection import get_session
from database.history_models import OptimizationResultRecord
from database.history_service import HistoryPersistenceService
from optimizer.optimization_result import OptimizationResult
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResumeState:
    execution_id: str
    processed: int
    completed: bool
    payload: dict[str, Any]


class HistoryListener(EventListener):
    """Persists optimizer lifecycle events using repository/service layer."""

    def __init__(self, checkpoint_interval: int = 50) -> None:
        self._checkpoint_interval = max(1, checkpoint_interval)
        self._last_checkpoint = 0

    def handle(self, event: OptimizationEvent) -> None:
        with get_session() as session:
            service = HistoryPersistenceService(session)
            if event.event_type == EventType.OPTIMIZER_STARTED:
                self._on_optimizer_started(service, event)
            elif event.event_type == EventType.COMBINATION_FINISHED:
                self._on_combination_finished(service, event)
            elif event.event_type == EventType.COMBINATION_SAVED:
                self._on_combination_saved(service, event)
            elif event.event_type == EventType.CHECKPOINT:
                self._on_checkpoint(service, event)
            elif event.event_type == EventType.OPTIMIZER_FINISHED:
                self._on_optimizer_finished(service, event)
            elif event.event_type == EventType.BACKTEST_FINISHED:
                self._on_backtest_finished(service, event)

    def _on_optimizer_started(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        payload = event.payload
        started_at = payload.get("started_at", event.timestamp)
        service.register_strategy_version(
            strategy_name=payload["strategy"],
            version=payload.get("strategy_version", "v1"),
            git_commit=payload.get("git_commit"),
            description=payload.get("strategy_description"),
        )
        service.start_execution_session(
            execution_id=event.execution_id,
            started_at=started_at,
            status="running",
            host=payload.get("host"),
            cpu=payload.get("cpu") or platform.processor() or None,
            workers=int(payload.get("workers", 1)),
            python_version=payload.get("python_version"),
            git_version=payload.get("git_commit"),
        )
        service.create_optimization_run(
            execution_id=event.execution_id,
            started_at=started_at,
            strategy=payload["strategy"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            total_combinations=int(payload.get("total_combinations", 0)),
            workers=int(payload.get("workers", 1)),
            status="running",
        )
        service.save_checkpoint(
            execution_id=event.execution_id,
            stage="optimizer",
            processed=0,
            completed=False,
            payload={"message": "optimizer_started"},
        )

    def _on_combination_finished(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        payload = event.payload
        result_payload = payload.get("result")
        if result_payload is None:
            return

        result = OptimizationResult(
            rank=result_payload.get("rank"),
            parameters=result_payload.get("parameters", {}),
            metrics=result_payload.get("metrics", {}),
            combinations_tested=result_payload.get("combinations_tested", 0),
            runtime_seconds=result_payload.get("runtime_seconds", 0.0),
            error=result_payload.get("error"),
        )
        service.save_optimization_result(
            execution_id=event.execution_id,
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            strategy=payload["strategy"],
            result=result,
            approved=False,
            rejection_reason=result.error,
        )

    def _on_combination_saved(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        processed = int(event.payload.get("processed", 0))
        if processed - self._last_checkpoint >= self._checkpoint_interval:
            self._last_checkpoint = processed
            service.save_checkpoint(
                execution_id=event.execution_id,
                stage="optimizer",
                processed=processed,
                completed=False,
                payload={
                    "last_rank": event.payload.get("last_rank"),
                    "parameters": event.payload.get("parameters"),
                    "timestamp": event.timestamp.isoformat(),
                },
            )

    def _on_checkpoint(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        service.save_checkpoint(
            execution_id=event.execution_id,
            stage=event.payload.get("stage", "optimizer"),
            processed=int(event.payload.get("processed", 0)),
            completed=bool(event.payload.get("completed", False)),
            payload=event.payload,
        )

    def _on_optimizer_finished(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        payload = event.payload
        safe_payload = dict(payload)
        finished_at = safe_payload.get("finished_at")
        if isinstance(finished_at, datetime):
            safe_payload["finished_at"] = finished_at.isoformat()
        actual_finished_at = payload.get("finished_at", event.timestamp)
        service.finish_execution_session(
            execution_id=event.execution_id,
            finished_at=actual_finished_at,
            duration=float(payload.get("duration_seconds", 0.0)),
            status=payload.get("status", "completed"),
        )
        service.finish_optimization_run(
            execution_id=event.execution_id,
            finished_at=actual_finished_at,
            duration_seconds=float(payload.get("duration_seconds", 0.0)),
            status=payload.get("status", "completed"),
        )
        service.save_checkpoint(
            execution_id=event.execution_id,
            stage="optimizer",
            processed=int(payload.get("processed", 0)),
            completed=True,
            payload=safe_payload,
        )

    def _on_backtest_finished(self, service: HistoryPersistenceService, event: OptimizationEvent) -> None:
        payload = event.payload
        service.create_backtest_run(
            execution_id=event.execution_id,
            strategy=payload["strategy"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            start_date=_coerce_datetime(payload.get("start_date"), event.timestamp),
            end_date=_coerce_datetime(payload.get("end_date"), event.timestamp),
            initial_capital=float(payload.get("initial_capital", 0.0)),
            final_capital=float(payload.get("final_capital", 0.0)) if payload.get("final_capital") is not None else None,
            total_trades=int(payload.get("total_trades", 0)) if payload.get("total_trades") is not None else None,
            win_rate=float(payload.get("win_rate", 0.0)) if payload.get("win_rate") is not None else None,
            profit_factor=float(payload.get("profit_factor", 0.0)) if payload.get("profit_factor") is not None else None,
            sharpe=float(payload.get("sharpe", 0.0)) if payload.get("sharpe") is not None else None,
            expectancy=float(payload.get("expectancy", 0.0)) if payload.get("expectancy") is not None else None,
            drawdown=float(payload.get("drawdown", 0.0)) if payload.get("drawdown") is not None else None,
            status=payload.get("status", "completed"),
        )

    @staticmethod
    def resume_execution(execution_id: str) -> ResumeState | None:
        with get_session() as session:
            service = HistoryPersistenceService(session)
            checkpoint = service.get_latest_checkpoint(execution_id, "optimizer")
            if checkpoint is None:
                return None
            result_count = session.scalar(
                select(func.count()).select_from(OptimizationResultRecord).where(
                    OptimizationResultRecord.execution_id == execution_id
                )
            ) or 0
            payload = {}
            if checkpoint.payload_json:
                try:
                    payload = json.loads(checkpoint.payload_json)
                except json.JSONDecodeError:
                    payload = {}
            return ResumeState(
                execution_id=execution_id,
                processed=max(int(checkpoint.processed), int(result_count)),
                completed=checkpoint.completed,
                payload=payload,
            )


class LogListener(EventListener):
    """Simple observability listener logging every event."""

    def handle(self, event: OptimizationEvent) -> None:
        logger.info(
            "event=%s execution_id=%s payload_keys=%s",
            event.event_type,
            event.execution_id,
            sorted(event.payload.keys()),
        )


class MetricsListener(EventListener):
    """In-memory real-time aggregate metrics for dashboards."""

    def __init__(self) -> None:
        self._state: dict[str, Any] = {
            "executed": 0,
            "remaining": 0,
            "best_profit_factor": 0.0,
            "best_sharpe": 0.0,
            "best_drawdown": None,
            "best_configuration": None,
        }

    def handle(self, event: OptimizationEvent) -> None:
        if event.event_type == EventType.OPTIMIZER_STARTED:
            total = int(event.payload.get("total_combinations", 0))
            self._state["executed"] = 0
            self._state["remaining"] = total
            return

        if event.event_type != EventType.COMBINATION_FINISHED:
            return

        self._state["executed"] += 1
        self._state["remaining"] = max(0, self._state["remaining"] - 1)

        result = event.payload.get("result", {})
        metrics = result.get("metrics", {})
        params = result.get("parameters", {})

        pf = float(metrics.get("profit_factor", 0.0) or 0.0)
        sharpe = float(metrics.get("sharpe_ratio", 0.0) or 0.0)
        drawdown = float(metrics.get("max_drawdown_pct", 0.0) or 0.0)

        if pf >= float(self._state["best_profit_factor"]):
            self._state["best_profit_factor"] = pf
            self._state["best_configuration"] = params
        if sharpe >= float(self._state["best_sharpe"]):
            self._state["best_sharpe"] = sharpe
        if self._state["best_drawdown"] is None or drawdown < float(self._state["best_drawdown"]):
            self._state["best_drawdown"] = drawdown

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)


def _coerce_datetime(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        return default
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return datetime.fromisoformat(str(value))
