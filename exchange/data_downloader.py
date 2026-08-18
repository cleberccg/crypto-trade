"""
Historical and real-time data downloader.

Design decision: DataDownloader is a high-level service that coordinates
between the exchange client and the database. It handles pagination for
large historical downloads and deduplicates inserts via CandleRepository.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from database.connection import get_session
from database.models import Candle
from database.repositories import CandleRepository
from exchange.base_exchange import BaseExchange
from utils.helpers import datetime_to_timestamp_ms, normalize_ohlcv_dataframe
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger(__name__)

# Binance returns at most 1000 candles per request
_MAX_CANDLES_PER_REQUEST = 1000


class DataDownloader:
    """
    Downloads OHLCV data from an exchange and persists it to the database.

    Usage::

        client = BinanceClient()
        client.connect()
        downloader = DataDownloader(client)
        df = downloader.download_historical("BTC/USDT", "1h", start, end)
    """

    def __init__(self, exchange: BaseExchange) -> None:
        self._exchange = exchange

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def download_historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """
        Download and persist historical OHLCV data for a date range.

        Paginates automatically to bypass the exchange's per-request limit.

        Args:
            symbol: Trading pair (e.g. ``BTC/USDT``).
            timeframe: Candle interval (e.g. ``1h``).
            start: Inclusive start datetime (UTC).
            end: Inclusive end datetime; defaults to now.

        Returns:
            Concatenated DataFrame of all downloaded candles.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        end = end or datetime.now(tz=timezone.utc)

        logger.info(
            "download_historical - symbol=%s tf=%s start=%s end=%s",
            symbol,
            timeframe,
            start.isoformat(),
            end.isoformat(),
        )

        frames: list[pd.DataFrame] = []
        total_inserted = 0

        for batch in self._paginate(symbol, timeframe, start, end):
            frames.append(batch)
            inserted = self._persist_batch(symbol, timeframe, batch)
            total_inserted += inserted

        if not frames:
            logger.warning("download_historical returned no data.")
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        result = pd.concat(frames).sort_index()
        result = result[~result.index.duplicated(keep="last")]

        logger.info(
            "download_historical complete - candles=%d new_rows=%d",
            len(result),
            total_inserted,
        )
        return result

    def get_latest_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """
        Fetch the most recent *limit* candles without persisting them.

        Useful for strategy signal generation without writing to the DB.

        Args:
            symbol: Trading pair.
            timeframe: Candle interval.
            limit: Number of most-recent candles.

        Returns:
            OHLCV DataFrame.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)

        logger.info(
            "get_latest_candles - symbol=%s tf=%s limit=%d", symbol, timeframe, limit
        )
        return self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    # ------------------------------------------------------------------
    # Auxiliares privados
    # ------------------------------------------------------------------

    def _paginate(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[pd.DataFrame]:
        """
        Yield DataFrame batches by paginating the exchange API.

        Stops when the batch's last timestamp exceeds *end* or the exchange
        returns fewer candles than requested (signalling exhaustion).
        """
        since_ms = datetime_to_timestamp_ms(start)
        end_ms = datetime_to_timestamp_ms(end)

        while True:
            batch = self._exchange.fetch_ohlcv(
                symbol,
                timeframe,
                since=since_ms,
                limit=_MAX_CANDLES_PER_REQUEST,
            )

            if batch.empty:
                logger.debug("_paginate - empty batch, stopping.")
                break

            # Limita linhas que excedem o tempo final solicitado
            batch = batch[batch.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]

            if batch.empty:
                break

            yield batch

            last_ts_ms = int(batch.index[-1].timestamp() * 1000)

            # Para quando atingir o limite final ou receber uma pagina parcial
            if last_ts_ms >= end_ms or len(batch) < _MAX_CANDLES_PER_REQUEST:
                break

            # Avanca o cursor apos o ultimo candle retornado
            since_ms = last_ts_ms + 1

    def _persist_batch(
        self, symbol: str, timeframe: str, df: pd.DataFrame
    ) -> int:
        """Convert a DataFrame batch to ORM objects and upsert them."""
        candles = [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=row.name.to_pydatetime(),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            for _, row in df.iterrows()
        ]

        with get_session() as session:
            repo = CandleRepository(session)
            return repo.upsert_many(candles)
