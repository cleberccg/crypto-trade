"""
Live order executor.

Design decision: OrderExecutor is intentionally minimal at this stage -
it wraps BinanceClient and persists orders to the database.  It will only
execute when PAPER_TRADING=false in .env, providing a hard safety gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database.connection import get_session
from database.models import Order, Trade
from database.repositories import TradeRepository
from exchange.base_exchange import BaseExchange
from utils.helpers import utc_now
from utils.logger import get_logger

logger = get_logger(__name__)


class OrderExecutor:
    """
    Executes live orders through the exchange client.

    SAFETY: Will raise RuntimeError when paper trading mode is active.
    The BinanceClient's _guard_live_trading() method enforces this at the
    network layer as well.

    Args:
        exchange: An initialised BaseExchange implementation.
    """

    def __init__(self, exchange: BaseExchange) -> None:
        self._exchange = exchange

    @property
    def exchange(self) -> BaseExchange:
        """Expose the configured exchange adapter."""
        return self._exchange

    def execute_market_buy(
        self,
        trade: Trade,
        symbol: str,
        quantity: float,
        price: float,
    ) -> Order:
        """
        Place a live market buy and persist the order record.

        Args:
            trade: The parent Trade ORM object (must already be persisted).
            symbol: Trading pair.
            quantity: Amount in base currency.
            price: Expected fill price (for record-keeping only).

        Returns:
            Persisted Order ORM object.
        """
        logger.warning(
            "Compra executada (envio de ordem) - symbol=%s qty=%.6f ~price=%.4f",
            symbol,
            quantity,
            price,
        )
        raw_order = self._exchange.create_market_order(symbol, "buy", quantity)

        fill_price = float(raw_order.get("average") or raw_order.get("price") or price)
        fill_qty = float(raw_order.get("filled", quantity))
        fee = float(raw_order.get("fee", {}).get("cost", 0.0))

        order = Order(
            trade_id=trade.id,
            exchange_order_id=str(raw_order["id"]),
            symbol=symbol,
            order_type="MARKET",
            side="BUY",
            status=raw_order.get("status", "filled").upper(),
            price=fill_price,
            quantity=quantity,
            filled_quantity=fill_qty,
            fee=fee,
            timestamp=utc_now(),
        )

        with get_session() as session:
            session.add(order)

        logger.info(
            "Compra executada - exchange_id=%s fill=%.4f qty=%.6f fee=%.4f",
            raw_order["id"],
            fill_price,
            fill_qty,
            fee,
        )
        return order

    def execute_market_sell(
        self,
        trade: Trade,
        symbol: str,
        quantity: float,
        price: float,
    ) -> Order:
        """
        Place a live market sell and persist the order record.

        Args:
            trade: The parent Trade ORM object.
            symbol: Trading pair.
            quantity: Amount in base currency to sell.
            price: Expected fill price (for record-keeping only).

        Returns:
            Persisted Order ORM object.
        """
        logger.warning(
            "Saida de posicao (stop/take/manual) - symbol=%s qty=%.6f ~price=%.4f",
            symbol,
            quantity,
            price,
        )
        raw_order = self._exchange.create_market_order(symbol, "sell", quantity)

        fill_price = float(raw_order.get("average") or raw_order.get("price") or price)
        fill_qty = float(raw_order.get("filled", quantity))
        fee = float(raw_order.get("fee", {}).get("cost", 0.0))

        order = Order(
            trade_id=trade.id,
            exchange_order_id=str(raw_order["id"]),
            symbol=symbol,
            order_type="MARKET",
            side="SELL",
            status=raw_order.get("status", "filled").upper(),
            price=fill_price,
            quantity=quantity,
            filled_quantity=fill_qty,
            fee=fee,
            timestamp=utc_now(),
        )

        with get_session() as session:
            session.add(order)

        logger.info(
            "Stop ou take profit executados (ou saida manual) - exchange_id=%s fill=%.4f qty=%.6f fee=%.4f",
            raw_order["id"],
            fill_price,
            fill_qty,
            fee,
        )
        return order
