"""
Executor de ordens ao vivo.

Decisão de projeto: neste estágio, OrderExecutor é intencionalmente mínimo:
encapsula BinanceClient e persiste as ordens no banco de dados. Só executa
quando PAPER_TRADING=false no .env, fornecendo uma barreira rígida de segurança.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database.connection import get_session
from database.models import Order, Trade
from exchange.base_exchange import BaseExchange
from utils.helpers import utc_now
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrderFill:
    quantity: float
    price: float
    fee_cost: float | None
    fee_currency: str | None
    fee_usdt: float | None
    fee_source: str
    fee_conversion_price: float | None = None
    base_fee_quantity: float = 0.0


@dataclass(frozen=True)
class OrderExecution:
    exchange_order_id: str
    status: str
    price: float
    quantity: float
    filled_quantity: float
    fee: float | None
    fee_currency: str | None
    fee_usdt: float | None
    fee_source: str
    base_fee_quantity: float
    fills: tuple[OrderFill, ...]


class OrderExecutor:
    """
    Executa ordens ao vivo por meio do cliente da exchange.

    SEGURANÇA: gera RuntimeError quando o modo paper trading está ativo.
    O método _guard_live_trading() de BinanceClient também aplica essa
    proteção na camada de rede.

    Argumentos:
        exchange: Uma implementação de BaseExchange inicializada.
    """

    def __init__(self, exchange: BaseExchange) -> None:
        self._exchange = exchange

    @property
    def exchange(self) -> BaseExchange:
        """Disponibiliza o adaptador de exchange configurado."""
        return self._exchange

    def execute_market_buy(
        self,
        trade: Trade,
        symbol: str,
        quantity: float,
        price: float,
    ) -> OrderExecution:
        """
        Envia uma compra a mercado ao vivo e persiste o registro da ordem.

        Argumentos:
            trade: Objeto ORM Trade relacionado (deve já estar persistido).
            symbol: Par de negociação.
            quantity: Quantidade na moeda base.
            price: Preço esperado de execução (somente para registro).

        Retorno:
            Objeto ORM Order persistido.
        """
        logger.warning(
            "Compra executada (envio de ordem) - symbol=%s qty=%.6f ~price=%.4f",
            symbol,
            quantity,
            price,
        )
        raw_order = self._exchange.create_market_order(symbol, "buy", quantity)

        return self._normalize_and_persist(
            raw_order=raw_order,
            trade_id=trade.id,
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            fallback_price=price,
        )

    def execute_market_sell(
        self,
        trade: Trade,
        symbol: str,
        quantity: float,
        price: float,
    ) -> OrderExecution:
        """
        Envia uma venda a mercado ao vivo e persiste o registro da ordem.

        Argumentos:
            trade: Objeto ORM Trade relacionado.
            symbol: Par de negociação.
            quantity: Quantidade na moeda base a vender.
            price: Preço esperado de execução (somente para registro).

        Retorno:
            Objeto ORM Order persistido.
        """
        logger.warning(
            "Saida de posicao (stop/take/manual) - symbol=%s qty=%.6f ~price=%.4f",
            symbol,
            quantity,
            price,
        )
        raw_order = self._exchange.create_market_order(symbol, "sell", quantity)

        return self._normalize_and_persist(
            raw_order=raw_order,
            trade_id=trade.id,
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            fallback_price=price,
        )

    def _normalize_and_persist(
        self,
        *,
        raw_order: dict[str, Any],
        trade_id: int,
        symbol: str,
        side: str,
        quantity: float,
        fallback_price: float,
    ) -> OrderExecution:
        exchange_order_id = str(raw_order.get("id") or "")
        status = str(raw_order.get("status") or "unknown").upper()
        fills = self._normalize_fills(raw_order, symbol, quantity, fallback_price)
        filled_quantity = sum(fill.quantity for fill in fills)
        priced_fills = [fill for fill in fills if fill.quantity > 0.0]
        fill_price = (
            sum(fill.quantity * fill.price for fill in priced_fills) / filled_quantity
            if filled_quantity > 0.0
            else float(raw_order.get("average") or raw_order.get("price") or fallback_price)
        )

        with get_session() as session:
            for fill in fills:
                session.add(
                    Order(
                        trade_id=trade_id,
                        exchange_order_id=exchange_order_id,
                        symbol=symbol,
                        order_type="MARKET",
                        side=side,
                        status=status,
                        price=fill.price,
                        quantity=fill.quantity,
                        filled_quantity=fill.quantity,
                        fee=float(fill.fee_cost or 0.0),
                        fee_currency=fill.fee_currency,
                        fee_usdt=fill.fee_usdt,
                        fee_source=fill.fee_source,
                        fee_conversion_price=fill.fee_conversion_price,
                        base_fee_quantity=fill.base_fee_quantity,
                        timestamp=utc_now(),
                    )
                )

        fee_currencies = {fill.fee_currency for fill in fills if fill.fee_currency}
        fee_costs = [fill.fee_cost for fill in fills]
        fee = (
            sum(float(cost) for cost in fee_costs if cost is not None)
            if len(fee_currencies) <= 1 and all(cost is not None for cost in fee_costs)
            else None
        )
        fee_usdt = (
            sum(float(fill.fee_usdt) for fill in fills if fill.fee_usdt is not None)
            if fills and all(fill.fee_usdt is not None for fill in fills)
            else None
        )
        missing_fee = any(fill.fee_source == "MISSING" for fill in fills)
        unconverted = any(fill.fee_source.endswith("UNCONVERTED") for fill in fills)
        fee_source = "MISSING" if missing_fee else "UNCONVERTED" if unconverted else "REPORTED"
        fee_currency = next(iter(fee_currencies)) if len(fee_currencies) == 1 else "MULTIPLE" if fee_currencies else None
        base_fee_quantity = sum(fill.base_fee_quantity for fill in fills)

        logger.info(
            "Order persisted - side=%s exchange_id=%s fills=%d executed=%.12f fee_usdt=%s source=%s",
            side,
            exchange_order_id,
            len(priced_fills),
            filled_quantity,
            f"{fee_usdt:.10f}" if fee_usdt is not None else "UNKNOWN",
            fee_source,
        )
        return OrderExecution(
            exchange_order_id=exchange_order_id,
            status=status,
            price=fill_price,
            quantity=float(quantity),
            filled_quantity=filled_quantity,
            fee=fee,
            fee_currency=fee_currency,
            fee_usdt=fee_usdt,
            fee_source=fee_source,
            base_fee_quantity=base_fee_quantity,
            fills=tuple(fills),
        )

    def _normalize_fills(
        self,
        raw_order: dict[str, Any],
        symbol: str,
        requested_quantity: float,
        fallback_price: float,
    ) -> list[OrderFill]:
        base_asset, quote_asset = (part.upper() for part in symbol.split("/", maxsplit=1))
        conversion_cache: dict[str, float | None] = {}
        info = raw_order.get("info") if isinstance(raw_order.get("info"), dict) else {}
        raw_fills = info.get("fills") if isinstance(info.get("fills"), list) else None
        if not raw_fills:
            raw_fills = raw_order.get("trades") if isinstance(raw_order.get("trades"), list) else None

        fill_parts: list[tuple[float, float, list[dict[str, Any]] | None]] = []
        if raw_fills:
            for item in raw_fills:
                if not isinstance(item, dict):
                    continue
                fill_qty = self._first_number(item, "qty", "amount", "quantity")
                fill_price = self._first_number(item, "price")
                if fill_qty <= 0.0 or fill_price <= 0.0:
                    continue
                fill_parts.append((fill_qty, fill_price, self._fee_entries(item)))

        if not fill_parts:
            fill_qty = max(0.0, float(raw_order.get("filled") or 0.0))
            if fill_qty > 0.0:
                fill_price = float(raw_order.get("average") or raw_order.get("price") or fallback_price)
                fill_parts.append((fill_qty, fill_price, None))

        order_fees = self._fee_entries(raw_order)
        if order_fees and fill_parts:
            missing_indexes = [
                index for index, (_, _, entries) in enumerate(fill_parts)
                if not entries or not any(
                    entry.get("cost") is not None and (entry.get("currency") or entry.get("commissionAsset"))
                    for entry in entries
                )
            ]
            if missing_indexes:
                known_by_currency: dict[str, float] = {}
                for _, _, entries in fill_parts:
                    for entry in entries or []:
                        currency_value = entry.get("currency") or entry.get("commissionAsset")
                        if currency_value and entry.get("cost") is not None:
                            currency = str(currency_value).upper()
                            known_by_currency[currency] = known_by_currency.get(currency, 0.0) + float(entry["cost"])

                remaining_fees: list[dict[str, Any]] = []
                for fee in order_fees:
                    currency_value = fee.get("currency") or fee.get("commissionAsset")
                    if not currency_value or fee.get("cost") is None:
                        continue
                    currency = str(currency_value).upper()
                    residual = max(0.0, float(fee["cost"]) - known_by_currency.get(currency, 0.0))
                    if residual > 0.0:
                        remaining_fees.append({"cost": residual, "currency": currency})

                missing_quantity = sum(fill_parts[index][0] for index in missing_indexes)
                if remaining_fees and missing_quantity > 0.0:
                    mutable_parts = list(fill_parts)
                    for index in missing_indexes:
                        fill_qty, fill_price, _ = mutable_parts[index]
                        allocated_fees = [
                            {
                                **fee,
                                "cost": float(fee["cost"]) * fill_qty / missing_quantity,
                                "source": "ORDER_ALLOCATED",
                            }
                            for fee in remaining_fees
                        ]
                        mutable_parts[index] = (fill_qty, fill_price, allocated_fees)
                    fill_parts = mutable_parts

        fills = []
        for fill_qty, fill_price, fees in fill_parts:
            fills.extend(self._make_fill_rows(
                fill_qty, fill_price, fees, base_asset, quote_asset, conversion_cache
            ))

        if not fill_parts:
            # Preserva o registro de uma ordem não executada; não substitui pela quantidade solicitada.
            zero_price = float(raw_order.get("average") or raw_order.get("price") or fallback_price)
            if order_fees:
                fills.extend(self._make_fill_rows(
                    0.0, zero_price, order_fees, base_asset, quote_asset, conversion_cache
                ))
            else:
                fills.append(OrderFill(0.0, zero_price, None, None, None, "MISSING"))
        return fills

    def _make_fill_rows(
        self,
        quantity: float,
        price: float,
        fee_entries: list[dict[str, Any]] | None,
        base_asset: str,
        quote_asset: str,
        conversion_cache: dict[str, float | None],
    ) -> list[OrderFill]:
        if not fee_entries:
            return [OrderFill(quantity, price, None, None, None, "MISSING")]

        normalized: list[OrderFill] = []
        remaining_quantity = quantity
        for entry in fee_entries:
            raw_cost = entry.get("cost")
            fee_cost = max(0.0, float(raw_cost)) if raw_cost is not None else None
            currency_value = entry.get("currency") or entry.get("commissionAsset")
            currency = str(currency_value).upper() if currency_value else None
            quantity_for_row = remaining_quantity
            remaining_quantity = 0.0
            fee_usdt, source, conversion_price, base_fee_qty = self._fee_to_usdt(
                fee_cost=fee_cost,
                currency=currency,
                fill_price=price,
                base_asset=base_asset,
                quote_asset=quote_asset,
                conversion_cache=conversion_cache,
                allocated=entry.get("source") == "ORDER_ALLOCATED",
            )
            normalized.append(
                OrderFill(
                    quantity=quantity_for_row,
                    price=price,
                    fee_cost=fee_cost,
                    fee_currency=currency,
                    fee_usdt=fee_usdt,
                    fee_source=source,
                    fee_conversion_price=conversion_price,
                    base_fee_quantity=base_fee_qty,
                )
            )
        return normalized

    def _fee_to_usdt(
        self,
        *,
        fee_cost: float | None,
        currency: str | None,
        fill_price: float,
        base_asset: str,
        quote_asset: str,
        conversion_cache: dict[str, float | None],
        allocated: bool,
    ) -> tuple[float | None, str, float | None, float]:
        if fee_cost is None or currency is None:
            return None, "MISSING", None, 0.0
        prefix = "ORDER_ALLOCATED" if allocated else "REPORTED"
        if currency == quote_asset:
            return fee_cost, prefix, None, 0.0
        if currency == base_asset:
            return fee_cost * fill_price, f"{prefix}_CONVERTED", fill_price, fee_cost

        if currency not in conversion_cache:
            conversion_cache[currency] = self._fetch_fee_conversion_price(currency, quote_asset)
        conversion_price = conversion_cache[currency]
        if conversion_price is None:
            return None, f"{prefix}_UNCONVERTED", None, 0.0
        return fee_cost * conversion_price, f"{prefix}_CONVERTED", conversion_price, 0.0

    def _fetch_fee_conversion_price(self, currency: str, quote_asset: str) -> float | None:
        direct_symbol = f"{currency}/{quote_asset}"
        try:
            price = float(self._exchange.fetch_ticker(direct_symbol).get("last") or 0.0)
            if price > 0.0:
                return price
        except Exception:
            pass
        inverse_symbol = f"{quote_asset}/{currency}"
        try:
            price = float(self._exchange.fetch_ticker(inverse_symbol).get("last") or 0.0)
            return 1.0 / price if price > 0.0 else None
        except Exception:
            return None

    @classmethod
    def _fee_entries(cls, source: dict[str, Any]) -> list[dict[str, Any]] | None:
        info = source.get("info") if isinstance(source.get("info"), dict) else {}
        if "commission" in source or "commission" in info:
            commission = source.get("commission", info.get("commission"))
            currency = source.get("commissionAsset", info.get("commissionAsset"))
            return [{"cost": commission, "currency": currency}]
        fees = source.get("fees")
        if isinstance(fees, list) and fees:
            return [
                {"cost": fee.get("cost"), "currency": fee.get("currency")}
                for fee in fees
                if isinstance(fee, dict)
            ]
        fee = source.get("fee")
        if isinstance(fee, dict):
            return [{"cost": fee.get("cost"), "currency": fee.get("currency")}]
        return None

    @staticmethod
    def _first_number(source: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = source.get(key)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _total_reported_fee(raw_order: dict) -> float:
        """Soma os valores de taxas informados pela exchange sem inventar taxas ausentes."""
        fees = raw_order.get("fees")
        if isinstance(fees, list) and fees:
            return sum(
                max(0.0, float(item.get("cost") or 0.0))
                for item in fees
                if isinstance(item, dict)
            )
        fee = raw_order.get("fee")
        return max(0.0, float(fee.get("cost") or 0.0)) if isinstance(fee, dict) else 0.0
