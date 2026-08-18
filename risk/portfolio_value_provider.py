"""
Portfolio value providers used by risk flows.

The goal is to keep the scientific sizing algorithm unchanged and only
change where portfolio_value comes from.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from exchange.base_exchange import BaseExchange

if TYPE_CHECKING:
    from paper_trading.paper_broker import PaperBroker


class PortfolioValueProvider(ABC):
    """Provides available portfolio value for opening new operations."""

    @abstractmethod
    def get_available_portfolio_value(self) -> Decimal:
        """Return available quote value for new operations."""


class PaperPortfolioValueProvider(PortfolioValueProvider):
    """Paper provider based on PaperBroker cash balance."""

    def __init__(self, broker: "PaperBroker") -> None:
        self._broker = broker

    def get_available_portfolio_value(self) -> Decimal:
        balance = self._broker.get_balance()
        return _to_decimal(balance.cash)


class BinancePortfolioValueProvider(PortfolioValueProvider):
    """
    Binance Spot provider based exclusively on free USDT.

    Never uses total/locked/used/equity/margin fields.
    """

    def __init__(self, exchange: BaseExchange, quote_asset: str = "USDT") -> None:
        self._exchange = exchange
        self._quote_asset = str(quote_asset or "USDT").upper()

    def get_available_portfolio_value(self) -> Decimal:
        balance = self._exchange.fetch_balance()
        if not isinstance(balance, dict):
            return Decimal("0")

        free_bucket = balance.get("free")
        if isinstance(free_bucket, dict):
            free_value = free_bucket.get(self._quote_asset, 0)
            return _to_decimal(free_value)

        # Fallback format often present in ccxt payloads: balance["USDT"]["free"]
        by_asset = balance.get(self._quote_asset)
        if isinstance(by_asset, dict):
            free_value = by_asset.get("free", 0)
            return _to_decimal(free_value)

        return Decimal("0")


def _to_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("0")
    if parsed < 0:
        return Decimal("0")
    return parsed
