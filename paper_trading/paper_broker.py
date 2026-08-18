"""
Paper broker - simulates order execution without touching the real exchange.

Design decision: PaperBroker mirrors the interface expected by any order
executor so that switching from paper to live trading requires no changes in
upper layers - only a different broker is injected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from utils.helpers import utc_now
from utils.logger import get_logger
from utils.validators import validate_positive_float

logger = get_logger(__name__)

_TAKER_FEE = 0.001  # 0.1%


@dataclass
class PaperOrder:
    """Represents a simulated order."""

    order_id: str
    symbol: str
    side: str  # buy | sell
    order_type: str  # mercado | limite
    quantity: float
    price: float
    filled_quantity: float = 0.0
    status: str = "open"  # open | filled | cancelled
    fee: float = 0.0
    timestamp: datetime = field(default_factory=utc_now)


@dataclass
class PaperBalance:
    """Current simulated portfolio balances."""

    cash: float
    positions: dict[str, float] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        """Total value including cash (positions are valued at cost basis here)."""
        return self.cash


class PaperBroker:
    """
    Simulates exchange order execution for paper trading.

    Args:
        initial_capital: Starting cash balance in quote currency.
        fee_pct: Simulated taker fee per fill (default 0.1%).
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        fee_pct: float = _TAKER_FEE,
    ) -> None:
        validate_positive_float(initial_capital, "initial_capital")
        self._balance = PaperBalance(cash=initial_capital)
        self._fee_pct = fee_pct
        self._orders: dict[str, PaperOrder] = {}
        self._order_counter = 0

        logger.info(
            "PaperBroker initialised - capital=%.2f fee=%.4f",
            initial_capital,
            fee_pct,
        )

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def get_balance(self) -> PaperBalance:
        """Return a copy of the current balance state."""
        return PaperBalance(
            cash=self._balance.cash,
            positions=dict(self._balance.positions),
        )

    def export_runtime_state(self) -> dict[str, Any]:
        """Export broker balance state for runtime resume."""
        return {
            "cash": float(self._balance.cash),
            "positions": {asset: float(qty) for asset, qty in self._balance.positions.items()},
        }

    def import_runtime_state(self, state: dict[str, Any] | None) -> None:
        """Restore broker balance state for runtime resume."""
        if not isinstance(state, dict):
            return

        cash = float(state.get("cash", self._balance.cash))
        raw_positions = state.get("positions")
        positions: dict[str, float] = {}
        if isinstance(raw_positions, dict):
            for asset, qty in raw_positions.items():
                token = str(asset or "").strip()
                value = float(qty)
                if token and value > 0.0:
                    positions[token] = value

        self._balance = PaperBalance(cash=max(0.0, cash), positions=positions)

    def get_position_quantity(self, symbol: str) -> float:
        """Return base-asset quantity currently held for a symbol."""
        base_asset = symbol.split("/")[0]
        return float(self._balance.positions.get(base_asset, 0.0))

    def get_portfolio_value(self, prices: dict[str, float]) -> float:
        """
        Calculate total portfolio value using current market prices.

        Args:
            prices: Dict mapping base currency to current price (e.g.
                    ``{"BTC": 42000.0}``).

        Returns:
            Total value in quote currency.
        """
        position_value = sum(
            qty * prices.get(asset, 0.0)
            for asset, qty in self._balance.positions.items()
        )
        return self._balance.cash + position_value

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def create_market_buy(self, symbol: str, quantity: float, price: float) -> PaperOrder:
        """
        Simulate a market buy order.

        Args:
            symbol: Trading pair (e.g. ``BTC/USDT``).
            quantity: Amount in base currency.
            price: Simulated fill price (typically the candle's close).

        Returns:
            Filled PaperOrder.
        """
        cost = quantity * price
        fee = cost * self._fee_pct
        total_cost = cost + fee

        if total_cost > self._balance.cash:
            raise ValueError(
                f"Insufficient cash: need {total_cost:.2f}, "
                f"have {self._balance.cash:.2f}."
            )

        self._balance.cash -= total_cost
        base_asset = symbol.split("/")[0]
        self._balance.positions[base_asset] = (
            self._balance.positions.get(base_asset, 0.0) + quantity
        )

        order = self._make_order(symbol, "buy", "market", quantity, price, fee)
        logger.info(
            "PaperBroker BUY - %s qty=%.6f @ %.4f fee=%.4f cash=%.2f",
            symbol,
            quantity,
            price,
            fee,
            self._balance.cash,
        )
        return order

    def create_market_sell(self, symbol: str, quantity: float, price: float) -> PaperOrder:
        """
        Simulate a market sell order.

        Args:
            symbol: Trading pair.
            quantity: Amount in base currency to sell.
            price: Simulated fill price.

        Returns:
            Filled PaperOrder.
        """
        base_asset = symbol.split("/")[0]
        available = self._balance.positions.get(base_asset, 0.0)

        if quantity > available:
            raise ValueError(
                f"Insufficient {base_asset}: need {quantity:.6f}, "
                f"have {available:.6f}."
            )

        proceeds = quantity * price
        fee = proceeds * self._fee_pct
        net_proceeds = proceeds - fee

        self._balance.positions[base_asset] = available - quantity
        if self._balance.positions[base_asset] <= 0:
            del self._balance.positions[base_asset]

        self._balance.cash += net_proceeds

        order = self._make_order(symbol, "sell", "market", quantity, price, fee)
        logger.info(
            "PaperBroker SELL - %s qty=%.6f @ %.4f fee=%.4f cash=%.2f",
            symbol,
            quantity,
            price,
            fee,
            self._balance.cash,
        )
        return order

    # ------------------------------------------------------------------
    # Auxiliares privados
    # ------------------------------------------------------------------

    def _make_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float,
        fee: float,
    ) -> PaperOrder:
        self._order_counter += 1
        order = PaperOrder(
            order_id=f"paper_{self._order_counter:06d}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            filled_quantity=quantity,
            status="filled",
            fee=fee,
        )
        self._orders[order.order_id] = order
        return order
