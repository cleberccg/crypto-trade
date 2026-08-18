"""
Abstract base class for exchange clients.

Design decision: Defining an interface via ABC ensures that alternative
exchange implementations (e.g. Kraken, Coinbase) can be swapped in without
changing the rest of the application (Dependency Inversion Principle).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class BaseExchange(ABC):
    """
    Abstract interface every exchange adapter must implement.

    All price/amount values use float. All timestamps use UTC.
    """

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Initialise connection / authenticate with the exchange."""

    @abstractmethod
    def disconnect(self) -> None:
        """Clean up connections and release resources."""

    # ------------------------------------------------------------------
    # Dados de mercado
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV (candlestick) data.

        Args:
            symbol: Trading pair, e.g. ``BTC/USDT``.
            timeframe: Candle interval, e.g. ``1h``.
            since: Start time as Unix timestamp in milliseconds.
            limit: Maximum number of candles to return.

        Returns:
            DataFrame with columns [open, high, low, close, volume]
            indexed by a UTC-aware DatetimeIndex.
        """

    @abstractmethod
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """
        Return the latest ticker information for *symbol*.

        The returned dict must include at least: ``last``, ``bid``, ``ask``,
        ``volume``, ``timestamp``.
        """

    @abstractmethod
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Return the current order book for *symbol*."""

    # ------------------------------------------------------------------
    # Conta
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_balance(self) -> dict[str, Any]:
        """Return account balances keyed by currency."""

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    @abstractmethod
    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """
        Place a market order.

        Args:
            symbol: Trading pair.
            side: ``buy`` or ``sell``.
            quantity: Amount in base currency.

        Returns:
            Exchange order response dict.
        """

    @abstractmethod
    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        """Place a limit order."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel an open order by its exchange-assigned ID."""

    @abstractmethod
    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Fetch the current state of a specific order."""

    @abstractmethod
    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return all currently open orders, optionally filtered by symbol."""
