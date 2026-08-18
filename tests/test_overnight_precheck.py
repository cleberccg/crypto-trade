from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.connection import DatabaseConnection
from database.models import Candle
from main import _run_overnight_campaign_precheck


def _sqlite_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def test_overnight_precheck_blocks_empty_database(tmp_path: Path) -> None:
    db_url = _sqlite_url(tmp_path, "precheck_empty.db")
    db = DatabaseConnection(db_url)
    try:
        db.create_tables()
    finally:
        db.dispose()

    report = _run_overnight_campaign_precheck(
        symbol="BTC/USDT",
        timeframe="5m",
        min_candles=1000,
        database_url=db_url,
    )

    assert report["status"] == "failed"
    reasons = [str(x) for x in report.get("fail_reasons", [])]
    assert any("Candles insuficientes" in reason for reason in reasons)
    assert int(report["candles"]["symbol_timeframe_count"]) == 0


def test_overnight_precheck_allows_database_with_required_candles(tmp_path: Path) -> None:
    db_url = _sqlite_url(tmp_path, "precheck_filled.db")
    db = DatabaseConnection(db_url)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    try:
        db.create_tables()
        candles: list[Candle] = []
        for i in range(1200):
            open_time = start + timedelta(minutes=5 * i)
            candles.append(
                Candle(
                    symbol="BTC/USDT",
                    timeframe="5m",
                    open_time=open_time,
                    open=100.0 + i,
                    high=101.0 + i,
                    low=99.0 + i,
                    close=100.5 + i,
                    volume=1000.0 + i,
                    close_time=open_time + timedelta(minutes=5),
                )
            )

        with db.session() as session:
            session.add_all(candles)
    finally:
        db.dispose()

    report = _run_overnight_campaign_precheck(
        symbol="BTC/USDT",
        timeframe="5m",
        min_candles=1000,
        database_url=db_url,
    )

    assert report["status"] == "ok"
    assert int(report["candles"]["symbol_timeframe_count"]) >= 1200
    assert report["db"]["type"] == "sqlite"
