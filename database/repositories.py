"""
Data-access repositories.

Design decision: Repository pattern separates query logic from domain logic.
Each repository wraps a single model and exposes typed query methods,
keeping raw SQLAlchemy out of the rest of the application.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import Candle, Order, PortfolioSnapshot, Signal, Trade
from utils.logger import get_logger

logger = get_logger(__name__)


class CandleRepository:
    """CRUD operations for Candle records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, candles: list[Candle]) -> int:
        """
        Insert candles that do not yet exist (duplicate-safe).

        Returns the number of rows actually inserted.
        """
        if not candles:
            return 0

        # Downloader batches are per symbol/timeframe; fetch existing timestamps
        # in one query instead of running one query per candle.
        symbol = candles[0].symbol
        timeframe = candles[0].timeframe
        open_times = [candle.open_time for candle in candles]

        existing_rows = (
            self._session.query(Candle.open_time)
            .filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time.in_(open_times),
            )
            .all()
        )
        existing_times = {row[0] for row in existing_rows}

        to_insert = [candle for candle in candles if candle.open_time not in existing_times]
        if to_insert:
            self._session.add_all(to_insert)
        inserted = len(to_insert)
        self._session.flush()
        logger.debug("CandleRepository.upsert_many - inserted %d rows.", inserted)
        return inserted

    def get_range(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return candles within [start, end] for the given symbol/timeframe."""
        return (
            self._session.query(Candle)
            .filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time <= end,
            )
            .order_by(Candle.open_time)
            .all()
        )

    def get_latest(self, symbol: str, timeframe: str) -> Optional[Candle]:
        """Return the most recent candle for the given symbol/timeframe."""
        return (
            self._session.query(Candle)
            .filter_by(symbol=symbol, timeframe=timeframe)
            .order_by(Candle.open_time.desc())
            .first()
        )


class TradeRepository:
    """CRUD operations for Trade records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, trade: Trade) -> Trade:
        """Persist a new trade and return it with its auto-generated ID."""
        self._session.add(trade)
        self._session.flush()
        logger.info("TradeRepository.create - trade id=%d symbol=%s", trade.id, trade.symbol)
        return trade

    def get_open_trades(self, symbol: Optional[str] = None) -> list[Trade]:
        """Return all trades with status OPEN, optionally filtered by symbol."""
        query = self._session.query(Trade).filter_by(status="OPEN")
        if symbol:
            query = query.filter_by(symbol=symbol)
        return query.all()

    def update(self, trade: Trade) -> Trade:
        """Merge changes back into the session."""
        self._session.merge(trade)
        self._session.flush()
        return trade

    def get_by_id(self, trade_id: int) -> Optional[Trade]:
        """Return a trade by its primary key."""
        return self._session.get(Trade, trade_id)

    def get_closed_trades(self, strategy_name: Optional[str] = None) -> list[Trade]:
        """Return all closed trades, optionally filtered by strategy."""
        query = self._session.query(Trade).filter_by(status="CLOSED")
        if strategy_name:
            query = query.filter_by(strategy_name=strategy_name)
        return query.order_by(Trade.entry_time).all()


class SignalRepository:
    """CRUD operations for Signal records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, signal: Signal) -> Signal:
        """Persist a new signal."""
        self._session.add(signal)
        self._session.flush()
        return signal

    def get_recent(self, symbol: str, strategy_name: str, limit: int = 10) -> list[Signal]:
        """Return the most recent signals for the given symbol and strategy."""
        return (
            self._session.query(Signal)
            .filter_by(symbol=symbol, strategy_name=strategy_name)
            .order_by(Signal.timestamp.desc())
            .limit(limit)
            .all()
        )


class PortfolioSnapshotRepository:
    """CRUD operations for PortfolioSnapshot records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        """Persist a new portfolio snapshot."""
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get_range(self, source: str, start: datetime, end: datetime) -> list[PortfolioSnapshot]:
        """Return snapshots within [start, end] for the given source."""
        return (
            self._session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.source == source,
                PortfolioSnapshot.timestamp >= start,
                PortfolioSnapshot.timestamp <= end,
            )
            .order_by(PortfolioSnapshot.timestamp)
            .all()
        )
