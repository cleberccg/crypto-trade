"""
Live risk service.

This module connects the live balance source (Binance Spot free USDT)
to the existing scientific risk pipeline without changing its formulas:

Binance -> PortfolioValueProvider -> RiskManager -> PositionSizer -> OrderExecutor
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any

from config.settings import settings
from database.models import Order, Trade
from execution.order_executor import OrderExecutor
from risk.portfolio_value_provider import BinancePortfolioValueProvider, PortfolioValueProvider
from risk.risk_manager import RiskManager, TradeRiskParams
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SymbolTradingFilters:
    min_notional: float
    min_qty: float
    step_size: float


@dataclass(frozen=True)
class LiveRiskExecutionResult:
    """Result of a live entry evaluated and executed with risk controls."""

    portfolio_value: float
    risk_params: TradeRiskParams
    order: Order


class LiveRiskService:
    """
    Orchestrates live entry execution through the same risk algorithm used in paper.

    Only the source of portfolio_value changes.
    """

    def __init__(
        self,
        order_executor: OrderExecutor,
        risk_manager: RiskManager | None = None,
        portfolio_value_provider: PortfolioValueProvider | None = None,
    ) -> None:
        self._order_executor = order_executor
        self._risk_manager = risk_manager or RiskManager()
        self._portfolio_value_provider = (
            portfolio_value_provider
            if portfolio_value_provider is not None
            else BinancePortfolioValueProvider(order_executor.exchange)
        )

    def execute_market_buy_with_risk(
        self,
        trade: Trade,
        symbol: str,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        trailing_stop_pct: float | None = None,
        strategy_score: float = 1.0,
        min_risk_reward_ratio: float | None = None,
    ) -> LiveRiskExecutionResult:
        """Evaluate and execute a live market buy using risk constraints."""
        portfolio_value = float(self._portfolio_value_provider.get_available_portfolio_value())
        if portfolio_value <= 0.0:
            raise ValueError("Insufficient available portfolio value for live order.")

        risk_params = self._risk_manager.evaluate_trade(
            portfolio_value=portfolio_value,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_pct=trailing_stop_pct,
            strategy_score=strategy_score,
            min_risk_reward_ratio=min_risk_reward_ratio,
        )

        if risk_params.stake_amount > portfolio_value:
            raise ValueError(
                "Insufficient free USDT for live order after risk sizing "
                f"(stake={risk_params.stake_amount:.6f}, free={portfolio_value:.6f})."
            )

        self._apply_small_account_adjustment(
            symbol=symbol,
            entry_price=entry_price,
            portfolio_value=portfolio_value,
            risk_params=risk_params,
        )

        order = self._order_executor.execute_market_buy(
            trade=trade,
            symbol=symbol,
            quantity=risk_params.quantity,
            price=entry_price,
        )
        return LiveRiskExecutionResult(
            portfolio_value=portfolio_value,
            risk_params=risk_params,
            order=order,
        )

    def _apply_small_account_adjustment(
        self,
        symbol: str,
        entry_price: float,
        portfolio_value: float,
        risk_params: TradeRiskParams,
    ) -> None:
        if not settings.small_account_mode:
            return

        filters = self._fetch_symbol_filters(symbol)
        stake_calculated = float(risk_params.stake_amount)
        min_notional = float(filters.min_notional)
        buffer_pct = max(0.01, float(settings.trading.min_notional_buffer_pct))
        min_buffered_notional = min_notional * (1.0 + buffer_pct)
        min_operational_stake = max(0.0, float(settings.trading.min_operational_stake_usdt))
        max_operational_stake = max(min_operational_stake, float(settings.trading.max_operational_stake_usdt))
        reserve_usdt = max(0.0, float(settings.trading.min_free_usdt_reserve))

        stake_floor = max(min_buffered_notional, min_operational_stake)
        if stake_floor <= max_operational_stake:
            stake_final = stake_floor
        else:
            stake_final = min_buffered_notional

        if stake_calculated >= stake_final and (portfolio_value - stake_calculated) >= reserve_usdt:
            logger.info("Stake calculado acima do minimo operacional.")
            return

        if portfolio_value < stake_final:
            raise ValueError(
                "Insufficient free USDT for SMALL_ACCOUNT_MODE minimum stake "
                f"(required={stake_final:.6f}, free={portfolio_value:.6f})."
            )

        qty_min_notional = self._ceil_to_step(stake_final / entry_price, filters.step_size)
        qty_min_qty = self._ceil_to_step(filters.min_qty, filters.step_size)
        quantity = max(qty_min_notional, qty_min_qty)
        stake_adjusted = quantity * entry_price

        if stake_adjusted < min_buffered_notional:
            quantity = self._ceil_to_step(min_buffered_notional / entry_price, filters.step_size)
            stake_adjusted = quantity * entry_price

        if portfolio_value < stake_adjusted:
            raise ValueError(
                "Insufficient free USDT after quantity rounding in SMALL_ACCOUNT_MODE "
                f"(required={stake_adjusted:.6f}, free={portfolio_value:.6f})."
            )

        if (portfolio_value - stake_adjusted) < reserve_usdt:
            raise ValueError(
                "Insufficient free USDT after reserve protection in SMALL_ACCOUNT_MODE "
                f"(required_reserve={reserve_usdt:.6f}, free_after_order={portfolio_value - stake_adjusted:.6f})."
            )

        logger.info(
            "Stake calculado: %.2f USDT | Stake minimo Binance: %.2f USDT | "
            "Stake minimo operacional: %.2f USDT | Buffer: %.2f%% | "
            "Modo: SMALL_ACCOUNT_MODE | Stake ajustado: %.2f USDT",
            stake_calculated,
            min_notional,
            stake_final,
            buffer_pct * 100.0,
            stake_adjusted,
        )

        risk_params.quantity = float(quantity)
        risk_params.stake_amount = float(stake_adjusted)
        risk_params.quantity_after_cap = float(quantity)

    def _fetch_symbol_filters(self, symbol: str) -> SymbolTradingFilters:
        exchange = self._order_executor.exchange

        if hasattr(exchange, "fetch_symbol_trading_filters"):
            raw_filters = exchange.fetch_symbol_trading_filters(symbol)  # type: ignore[attr-defined]
            return SymbolTradingFilters(
                min_notional=float(raw_filters["min_notional"]),
                min_qty=float(raw_filters["min_qty"]),
                step_size=float(raw_filters["step_size"]),
            )

        raise RuntimeError(
            "SMALL_ACCOUNT_MODE requires exchange filter support via "
            "fetch_symbol_trading_filters(symbol)."
        )

    @staticmethod
    def _ceil_to_step(value: float, step_size: float) -> float:
        if step_size <= 0:
            return float(value)

        val = Decimal(str(value))
        step = Decimal(str(step_size))
        units = (val / step).to_integral_value(rounding=ROUND_CEILING)
        adjusted = units * step
        return float(adjusted)
