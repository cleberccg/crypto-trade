"""
Binance Spot market-data client (live, public, read-only).

This adapter is dedicated to historical/public data collection so research
pipelines are independent from execution/testnet settings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd
import requests

from exchange.base_exchange import BaseExchange
from utils.helpers import normalize_ohlcv_dataframe, retry, timeit
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger(__name__)


class BinanceMarketDataClient(BaseExchange):
    """Read-only Binance Spot client using live public endpoints."""

    def __init__(self) -> None:
        self._exchange: ccxt.binance | None = None

    def connect(self) -> None:
        # Public OHLCV/ticker endpoints do not require API keys.
        self._exchange = ccxt.binance(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self._exchange.load_markets()
        logger.info("BinanceMarketDataClient connected in LIVE PUBLIC mode.")

    def disconnect(self) -> None:
        if self._exchange is not None:
            logger.info("BinanceMarketDataClient disconnected.")
            self._exchange = None

    @property
    def _client(self) -> ccxt.binance:
        if self._exchange is None:
            raise RuntimeError(
                "BinanceMarketDataClient is not connected. Call connect() first."
            )
        return self._exchange

    def is_symbol_supported(self, symbol: str) -> bool:
        symbol = validate_symbol(symbol)
        return symbol in self._client.markets

    @timeit
    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)

        logger.info(
            "fetch_ohlcv (live public) - symbol=%s timeframe=%s since=%s limit=%s",
            symbol,
            timeframe,
            since,
            limit,
        )

        raw = self._client.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit or 500,
        )
        if not raw:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.drop(columns=["timestamp"], inplace=True)
        return normalize_ohlcv_dataframe(df)

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        symbol = validate_symbol(symbol)
        ticker = self._client.fetch_ticker(symbol)
        logger.debug("fetch_ticker (live public) - symbol=%s last=%s", symbol, ticker.get("last"))
        return ticker

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        symbol = validate_symbol(symbol)
        return self._client.fetch_order_book(symbol, limit=limit)

    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_aggtrades_page(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch one deterministic page of Binance Spot aggregated trades."""
        symbol = validate_symbol(symbol)
        params: dict[str, Any] = {"symbol": symbol.replace("/", ""), "limit": min(1000, max(1, int(limit)))}
        if from_id is not None:
            params["fromId"] = int(from_id)
        else:
            params["startTime"] = int(start.astimezone(timezone.utc).timestamp() * 1000)
            params["endTime"] = int(end.astimezone(timezone.utc).timestamp() * 1000)
        response = requests.get(
            "https://api.binance.com/api/v3/aggTrades",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected aggTrades response: {payload}")
        return payload

    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_extended_klines(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch Binance Spot klines including trade-flow fields.

        The response is still candle-based and uses only closed klines. The
        extra fields are quote volume, trade count and taker-buy volumes.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        interval_ms = int((pd.Timedelta(timeframe).total_seconds()) * 1000)
        cursor = int(start.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(end.astimezone(timezone.utc).timestamp() * 1000)
        rows: list[list[Any]] = []
        session = requests.Session()
        while cursor <= end_ms:
            response = session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol.replace("/", ""), "interval": timeframe, "startTime": cursor, "endTime": end_ms, "limit": 1000},
                timeout=30,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            if len(batch) < 1000 or last_open >= end_ms:
                break
            cursor = last_open + interval_ms
        columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trade_count", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"]
        frame = pd.DataFrame(rows, columns=columns)
        if frame.empty:
            return frame
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True)
        numeric = [column for column in columns if column not in {"open_time", "close_time", "ignore"}]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        return frame.drop(columns=["ignore"]).drop_duplicates("open_time").set_index("open_time").sort_index()

    def fetch_balance(self) -> dict[str, Any]:
        raise RuntimeError("Read-only market data client does not expose account balance.")

    def create_market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any]:
        raise RuntimeError("Read-only market data client cannot place orders.")

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        raise RuntimeError("Read-only market data client cannot place orders.")

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        raise RuntimeError("Read-only market data client cannot cancel orders.")

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        raise RuntimeError("Read-only market data client cannot fetch private order state.")

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("Read-only market data client cannot fetch open orders.")
