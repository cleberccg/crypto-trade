"""
Database smoke tests.

These tests validate the SQLAlchemy integration layer without requiring a
running MySQL instance. The goal is to prove that the project can create an
engine, build tables, and persist a simple model through the repository
boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database.bootstrap import bootstrap_database
from database.connection import DatabaseConnection
from database.models import Candle
from database.repositories import CandleRepository


def test_database_connection_creates_tables_and_persists_candles() -> None:
    """SQLite smoke test for the DB layer."""
    db = DatabaseConnection("sqlite:///:memory:")
    try:
        db.create_tables()

        candle = Candle(
            symbol="BTC/USDT",
            timeframe="1h",
            open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=110.0,
            low=95.0,
            close=105.0,
            volume=1234.5,
        )

        with db.session() as session:
            repo = CandleRepository(session)
            inserted = repo.upsert_many([candle])
            assert inserted == 1

        with db.session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(
                symbol="BTC/USDT",
                timeframe="1h",
                start=datetime(2023, 12, 31, tzinfo=timezone.utc),
                end=datetime(2024, 1, 2, tzinfo=timezone.utc),
            )

        assert len(candles) == 1
        assert candles[0].close == 105.0
    finally:
        db.dispose()


def test_bootstrap_database_with_sqlite_memory() -> None:
    """Bootstrap should work end-to-end for SQLite without external services."""
    result = bootstrap_database("sqlite:///:memory:")

    assert result.database_created is False
    assert result.tables_created is True
    assert result.database_url.startswith("sqlite:")
