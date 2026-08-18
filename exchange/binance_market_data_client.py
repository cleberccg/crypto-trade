"""
Binance Spot market-data client (live, public, read-only).

This adapter is dedicated to historical/public data collection so research
pipelines are independent from execution/testnet settings.
"""
from __future__ import annotations

from typing import Any

import ccxt
import pandas as pd

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
