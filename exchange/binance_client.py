"""
Binance exchange adapter built on top of ccxt.

Design decision: ccxt is used as the primary library because it normalises
responses across 100+ exchanges. python-binance is kept as an optional
dependency for Binance-specific WebSocket streams not covered by ccxt.
"""
from __future__ import annotations

import os
from typing import Any

import ccxt
import pandas as pd

from config.settings import settings
from exchange.base_exchange import BaseExchange
from utils.helpers import normalize_ohlcv_dataframe, retry, timeit, timestamp_to_datetime
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger(__name__)


class BinanceClient(BaseExchange):
    """
    Binance exchange adapter.

    Uses ccxt under the hood, with testnet support via the ``sandbox`` flag.
    All public methods log entry, exit and any errors so that every API
    interaction is fully auditable.
    """

    def __init__(self) -> None:
        self._exchange: ccxt.binance | None = None

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Initialise the ccxt Binance instance and validate credentials."""
        cfg = settings.binance
        recv_window_ms = max(5000, int(os.getenv("BINANCE_RECV_WINDOW_MS", "60000")))
        self._exchange = ccxt.binance(
            {
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                    "recvWindow": recv_window_ms,
                },
            }
        )

        if cfg.testnet:
            self._exchange.set_sandbox_mode(True)
            logger.info("BinanceClient connected in TESTNET mode.")
        else:
            logger.info("BinanceClient connected in LIVE mode.")

        # Eager-load markets so subsequent calls don't trigger extra requests
        self._exchange.load_markets()
        self._sync_time_offset(context="connect")
        logger.debug("Markets loaded - %d symbols available.", len(self._exchange.markets))

    def disconnect(self) -> None:
        """Release any open sessions."""
        if self._exchange:
            # O ccxt nao mantem conexoes persistentes, mas chamar close
            # e uma boa pratica para garantir compatibilidade futura.
            logger.info("BinanceClient disconnected.")
            self._exchange = None

    @property
    def _client(self) -> ccxt.binance:
        """Return the underlying ccxt client, raising if not connected."""
        if self._exchange is None:
            raise RuntimeError(
                "BinanceClient is not connected. Call connect() first."
            )
        return self._exchange

    def is_symbol_supported(self, symbol: str) -> bool:
        """Return True when the symbol exists in loaded Binance markets."""
        symbol = validate_symbol(symbol)
        return symbol in self._client.markets

    # ------------------------------------------------------------------
    # Dados de mercado
    # ------------------------------------------------------------------

    @timeit
    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data and return a normalised DataFrame.

        Args:
            symbol: Trading pair (e.g. ``BTC/USDT``).
            timeframe: Candle interval (e.g. ``1h``).
            since: Start time in milliseconds UTC.
            limit: Max candles to return (Binance max = 1000).

        Returns:
            Normalised OHLCV DataFrame indexed by UTC DatetimeIndex.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)

        logger.info(
            "fetch_ohlcv - symbol=%s timeframe=%s since=%s limit=%s",
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
            logger.warning("fetch_ohlcv returned empty data for %s/%s.", symbol, timeframe)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.drop(columns=["timestamp"], inplace=True)

        return normalize_ohlcv_dataframe(df)

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Return the latest ticker for *symbol*."""
        symbol = validate_symbol(symbol)
        ticker = self._client.fetch_ticker(symbol)
        logger.debug("fetch_ticker - symbol=%s last=%s", symbol, ticker.get("last"))
        return ticker

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Return the order book for *symbol*."""
        symbol = validate_symbol(symbol)
        return self._client.fetch_order_book(symbol, limit=limit)

    # ------------------------------------------------------------------
    # Conta
    # ------------------------------------------------------------------

    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_balance(self) -> dict[str, Any]:
        """Return the account balance."""
        try:
            balance = self._client.fetch_balance()
        except Exception as exc:
            if self._is_timestamp_window_error(exc):
                logger.warning(
                    "Binance returned timestamp/recvWindow error on fetch_balance; syncing clock and retrying once."
                )
                self._sync_time_offset(context="fetch_balance")
                balance = self._client.fetch_balance()
            else:
                raise
        logger.debug("fetch_balance - total currencies: %d", len(balance.get("total", {})))
        return balance

    def _sync_time_offset(self, context: str) -> None:
        """Synchronize local/server clock offset in ccxt when available."""
        try:
            offset_ms = self._client.load_time_difference()
            logger.info("Binance time offset synced (%s): %sms", context, offset_ms)
        except Exception as exc:
            logger.warning("Unable to sync Binance time offset (%s): %s", context, exc)

    @staticmethod
    def _is_timestamp_window_error(exc: Exception) -> bool:
        message = str(exc)
        return "-1021" in message or "outside of the recvWindow" in message

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """Place a market order."""
        self._guard_live_trading()
        symbol = validate_symbol(symbol)
        logger.info(
            "create_market_order - symbol=%s side=%s qty=%s", symbol, side, quantity
        )
        order = self._client.create_order(symbol, "market", side, quantity)
        logger.info("Market order placed - id=%s status=%s", order["id"], order["status"])
        return order

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        """Place a limit order."""
        self._guard_live_trading()
        symbol = validate_symbol(symbol)
        logger.info(
            "create_limit_order - symbol=%s side=%s qty=%s price=%s",
            symbol,
            side,
            quantity,
            price,
        )
        order = self._client.create_order(symbol, "limit", side, quantity, price)
        logger.info("Limit order placed - id=%s status=%s", order["id"], order["status"])
        return order

    @retry(max_attempts=3, delay_seconds=1.0)
    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel an open order."""
        symbol = validate_symbol(symbol)
        logger.info("cancel_order - id=%s symbol=%s", order_id, symbol)
        result = self._client.cancel_order(order_id, symbol)
        logger.info("Order cancelled - id=%s", order_id)
        return result

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Fetch a specific order's current state."""
        symbol = validate_symbol(symbol)
        return self._client.fetch_order(order_id, symbol)

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return open orders, optionally filtered by symbol."""
        if symbol:
            symbol = validate_symbol(symbol)
        orders = self._client.fetch_open_orders(symbol)
        logger.debug("fetch_open_orders - found %d open orders.", len(orders))
        return orders

    def fetch_symbol_trading_filters(self, symbol: str) -> dict[str, float]:
        """Return Binance official trading filters for a symbol."""
        symbol = validate_symbol(symbol)
        market = self._client.market(symbol)

        limits = market.get("limits", {}) or {}
        amount_limits = limits.get("amount", {}) or {}
        cost_limits = limits.get("cost", {}) or {}

        min_qty = float(amount_limits.get("min") or 0.0)
        min_notional = float(cost_limits.get("min") or 0.0)

        info = market.get("info", {}) or {}
        filters = info.get("filters", []) or []
        step_size = 0.0
        for item in filters:
            if item.get("filterType") == "LOT_SIZE":
                step_size = float(item.get("stepSize") or 0.0)
                if min_qty <= 0.0:
                    min_qty = float(item.get("minQty") or 0.0)
            if item.get("filterType") == "NOTIONAL" and min_notional <= 0.0:
                min_notional = float(item.get("minNotional") or 0.0)
            if item.get("filterType") == "MIN_NOTIONAL" and min_notional <= 0.0:
                min_notional = float(item.get("minNotional") or 0.0)

        if step_size <= 0.0:
            precision = market.get("precision", {}) or {}
            amount_precision = precision.get("amount")
            if amount_precision:
                step_size = float(amount_precision)

        if min_notional <= 0.0 or min_qty <= 0.0 or step_size <= 0.0:
            raise RuntimeError(
                "Unable to resolve Binance trading filters "
                f"for {symbol}: min_notional={min_notional} min_qty={min_qty} step_size={step_size}"
            )

        return {
            "min_notional": min_notional,
            "min_qty": min_qty,
            "step_size": step_size,
        }

    # ------------------------------------------------------------------
    # Protecoes
    # ------------------------------------------------------------------

    def _guard_live_trading(self) -> None:
        """
        Raise RuntimeError when paper trading mode is active.

        This prevents accidental live order placement during testing.
        """
        if settings.is_paper_trading:
            raise RuntimeError(
                "Live order rejected: paper trading mode is active. "
                "Set PAPER_TRADING=false in .env to enable real orders."
            )
