"""Tests checkpoint persistence and resume state from HistoryListener."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from core.events.events import EventType, OptimizationEvent
from core.events.listeners import HistoryListener
from database import connection as connection_module
from database.connection import DatabaseConnection
from database.history_models import OptimizationResultRecord
from database.session_models import ExecutionSession, StrategyVersion


def test_history_listener_checkpoint_and_resume_sqlite() -> None:
    db = DatabaseConnection("sqlite:///:memory:")
    previous_db = connection_module._db
    try:
        connection_module._db = db
        db.create_tables()

        listener = HistoryListener(checkpoint_interval=1)
        execution_id = f"exec-resume-{uuid4()}"

        listener.handle(
            OptimizationEvent(
                event_type=EventType.OPTIMIZER_STARTED,
                execution_id=execution_id,
                payload={
                    "started_at": datetime.now(tz=timezone.utc),
                    "strategy": "TrendV1",
                    "symbol": "BTC/USDT",
                    "timeframe": "5m",
                    "total_combinations": 10,
                    "workers": 1,
                },
            )
        )

        listener.handle(
            OptimizationEvent(
                event_type=EventType.CHECKPOINT,
                execution_id=execution_id,
                payload={
                    "stage": "optimizer",
                    "processed": 3,
                    "completed": False,
                    "parameters": {"ema_fast": 9},
                },
            )
        )

        state = listener.resume_execution(execution_id)
        assert state is not None
        assert state.execution_id == execution_id
        assert state.processed == 3
        assert state.completed is False

        listener.handle(
            OptimizationEvent(
                event_type=EventType.OPTIMIZER_FINISHED,
                execution_id=execution_id,
                payload={
                    "finished_at": datetime.now(tz=timezone.utc),
                    "duration_seconds": 12.5,
                    "processed": 3,
                    "status": "completed",
                },
            )
        )

        with db.session() as session:
            session_record = session.query(ExecutionSession).filter_by(execution_id=execution_id).one()
            strategy_version = session.query(StrategyVersion).filter_by(strategy_name="TrendV1", version="v1").one()

            assert session_record.status == "completed"
            assert session_record.duration == 12.5
            assert session_record.finished_at is not None
            assert strategy_version.active is True

        listener.handle(
            OptimizationEvent(
                event_type=EventType.OPTIMIZER_STARTED,
                execution_id=execution_id,
                payload={
                    "started_at": datetime.now(tz=timezone.utc),
                    "strategy": "TrendV1",
                    "strategy_version": "v1",
                    "symbol": "BTC/USDT",
                    "timeframe": "5m",
                    "total_combinations": 10,
                    "workers": 1,
                },
            )
        )

        with db.session() as session:
            session_count = session.query(ExecutionSession).filter_by(execution_id=execution_id).count()
            strategy_count = session.query(StrategyVersion).filter_by(strategy_name="TrendV1", version="v1").count()
            assert session_count == 1
            assert strategy_count == 1
    finally:
        connection_module._db = previous_db
        db.dispose()


def test_history_listener_resume_prefers_persisted_results_when_checkpoint_lags() -> None:
    db = DatabaseConnection("sqlite:///:memory:")
    previous_db = connection_module._db
    try:
        connection_module._db = db
        db.create_tables()

        listener = HistoryListener(checkpoint_interval=10)
        execution_id = f"exec-resume-lag-{uuid4()}"

        listener.handle(
            OptimizationEvent(
                event_type=EventType.OPTIMIZER_STARTED,
                execution_id=execution_id,
                payload={
                    "started_at": datetime.now(tz=timezone.utc),
                    "strategy": "TrendV1",
                    "symbol": "BTC/USDT",
                    "timeframe": "5m",
                    "total_combinations": 100,
                    "workers": 1,
                },
            )
        )

        with db.session() as session:
            for idx in range(7):
                session.add(
                    OptimizationResultRecord(
                        execution_id=execution_id,
                        strategy="TrendV1",
                        symbol="BTC/USDT",
                        timeframe="5m",
                        parameters_json=f'{{"i": {idx}}}',
                        approved=False,
                    )
                )

        state = listener.resume_execution(execution_id)
        assert state is not None
        assert state.processed == 7
        assert state.completed is False
    finally:
        connection_module._db = previous_db
        db.dispose()
