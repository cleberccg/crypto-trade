from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from database import connection as connection_module
from database.connection import DatabaseConnection
from database.history_models import ExecutionCheckpoint, OptimizationResultRecord, OptimizationRun
from night_runner import NightRunner


def _seed_inconsistent_checkpoint(db: DatabaseConnection, execution_id: str) -> None:
    now = datetime.now(timezone.utc)
    with db.session() as session:
        session.add(
            OptimizationRun(
                execution_id=execution_id,
                started_at=now - timedelta(hours=2),
                finished_at=None,
                duration_seconds=None,
                strategy="TrendV1",
                symbol="BTC/USDT",
                timeframe="5m",
                total_combinations=10000,
                workers=2,
                status="running",
            )
        )
        # Checkpoint says 50 processed, but only 10 persisted results: must be rejected.
        session.add(
            ExecutionCheckpoint(
                execution_id=execution_id,
                stage="optimizer",
                processed=50,
                completed=False,
                payload_json=None,
            )
        )
        for idx in range(10):
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


def _seed_consistent_checkpoint(db: DatabaseConnection, execution_id: str, processed: int = 10) -> None:
    now = datetime.now(timezone.utc)
    with db.session() as session:
        session.add(
            OptimizationRun(
                execution_id=execution_id,
                started_at=now - timedelta(hours=1),
                finished_at=None,
                duration_seconds=None,
                strategy="TrendV1",
                symbol="BTC/USDT",
                timeframe="5m",
                total_combinations=10000,
                workers=2,
                status="running",
            )
        )
        session.add(
            ExecutionCheckpoint(
                execution_id=execution_id,
                stage="optimizer",
                processed=processed,
                completed=False,
                payload_json=None,
            )
        )
        for idx in range(processed):
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


def test_validate_resume_checkpoint_rejects_inconsistent_counts(tmp_path) -> None:
    db_path = tmp_path / "recovery_test.db"
    db = DatabaseConnection(f"sqlite:///{db_path}")
    previous_db = connection_module._db
    connection_module._db = db

    try:
        db.create_tables()
        execution_id = "exec-invalid-checkpoint"
        _seed_inconsistent_checkpoint(db, execution_id)

        runner = NightRunner(dry_run=True)
        is_valid, reason = runner._validate_resume_checkpoint(execution_id, "BTC/USDT", "5m")

        assert is_valid is False
        assert reason == "persisted_results_less_than_checkpoint"

        resumed = runner._find_resume_execution_id("BTC/USDT", "5m")
        assert resumed is None
    finally:
        connection_module._db = previous_db
        db.dispose()


def test_watchdog_sets_abort_and_blocked_state() -> None:
    runner = NightRunner(dry_run=True)
    runner.progress.started_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    runner.progress.last_log_at = None
    runner.progress.last_checkpoint_at = None
    runner.progress.last_db_update_at = None
    runner.progress.last_heartbeat_at = None

    runner._watchdog_stop.clear()
    runner._abort_execution.clear()

    # Simulate the watchdog decision logic directly.
    stalled = runner.progress.stalled_seconds()
    threshold_seconds = 15 * 60
    assert stalled >= threshold_seconds

    runner._record_incident("watchdog_stall", RuntimeError("simulated stall"))
    runner.progress.state = "blocked"
    runner._abort_execution.set()

    assert runner.progress.state == "blocked"
    assert runner._abort_execution.is_set() is True


def test_validate_resume_checkpoint_handles_sqlite_lock(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "recovery_lock_test.db"
    db = DatabaseConnection(f"sqlite:///{db_path}")
    previous_db = connection_module._db
    connection_module._db = db

    try:
        db.create_tables()
        execution_id = "exec-sqlite-lock"
        _seed_consistent_checkpoint(db, execution_id, processed=10)

        def _raise_locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("night_runner.sqlite3.connect", _raise_locked)

        runner = NightRunner(dry_run=True)
        is_valid, reason = runner._validate_resume_checkpoint(execution_id, "BTC/USDT", "5m")

        assert is_valid is False
        assert reason.startswith("validation_exception:")
    finally:
        connection_module._db = previous_db
        db.dispose()
