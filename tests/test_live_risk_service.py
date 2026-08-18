from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from config.settings import settings
from execution.live_risk_service import LiveRiskService
from risk.portfolio_value_provider import PortfolioValueProvider
from risk.risk_manager import RiskManager, TradeRiskParams


class _StaticPortfolioProvider(PortfolioValueProvider):
    def __init__(self, value: str) -> None:
        self._value = Decimal(value)

    def get_available_portfolio_value(self) -> Decimal:
        return self._value


@dataclass
class _FakeTrade:
    id: int = 1


class _FakeOrderExecutor:
    def __init__(self, exchange: object | None = None) -> None:
        self.calls: list[dict[str, float | str | int]] = []
        self.exchange = exchange if exchange is not None else object()

    def execute_market_buy(self, trade, symbol: str, quantity: float, price: float):
        self.calls.append(
            {
                "trade_id": int(getattr(trade, "id", 0) or 0),
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
            }
        )
        return {"id": "fake-order"}


class _FakeExchangeWithFilters:
    def __init__(
        self,
        min_notional: float = 5.0,
        min_qty: float = 0.00001,
        step_size: float = 0.00001,
    ) -> None:
        self._filters = {
            "min_notional": min_notional,
            "min_qty": min_qty,
            "step_size": step_size,
        }
        self.calls: list[str] = []

    def fetch_symbol_trading_filters(self, symbol: str) -> dict[str, float]:
        self.calls.append(symbol)
        return dict(self._filters)


class _FixedRiskManager:
    def __init__(self, stake_amount: float, quantity: float) -> None:
        self._stake_amount = stake_amount
        self._quantity = quantity

    def evaluate_trade(self, **kwargs) -> TradeRiskParams:
        return TradeRiskParams(
            quantity=self._quantity,
            stake_amount=self._stake_amount,
            stop_loss=95.0,
            take_profit=110.0,
            trailing_stop_pct=0.015,
            risk_amount=1.0,
            risk_pct=0.01,
            reward_amount=2.0,
            risk_reward_ratio=2.0,
            quantity_suggested=self._quantity,
            quantity_after_cap=self._quantity,
            max_stake=9999.0,
            was_capped=False,
        )


@pytest.fixture(autouse=True)
def _restore_small_account_mode() -> None:
    original = settings.small_account_mode
    try:
        yield
    finally:
        settings.small_account_mode = original


def test_insufficient_balance_prevents_order_open() -> None:
    settings.small_account_mode = False
    executor = _FakeOrderExecutor()
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=RiskManager(),
        portfolio_value_provider=_StaticPortfolioProvider("0"),
    )

    with pytest.raises(ValueError, match="Insufficient available portfolio value"):
        service.execute_market_buy_with_risk(
            trade=_FakeTrade(),
            symbol="BTC/USDT",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
        )

    assert executor.calls == []


def test_live_sizing_matches_paper_when_portfolio_value_is_equal() -> None:
    settings.small_account_mode = False
    # Same risk manager algorithm and same portfolio_value must produce identical sizing.
    portfolio_value = 10_000.0
    entry_price = 100.0
    stop_loss = 95.0
    take_profit = 110.0
    strategy_score = 1.0

    risk_manager = RiskManager()
    executor = _FakeOrderExecutor()
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=risk_manager,
        portfolio_value_provider=_StaticPortfolioProvider(str(portfolio_value)),
    )

    live_result = service.execute_market_buy_with_risk(
        trade=_FakeTrade(),
        symbol="BTC/USDT",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        strategy_score=strategy_score,
    )

    paper_like_params = risk_manager.evaluate_trade(
        portfolio_value=portfolio_value,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        strategy_score=strategy_score,
    )

    assert live_result.risk_params.quantity == pytest.approx(paper_like_params.quantity)
    assert live_result.risk_params.stake_amount == pytest.approx(paper_like_params.stake_amount)
    assert live_result.risk_params.risk_amount == pytest.approx(paper_like_params.risk_amount)
    assert live_result.risk_params.risk_reward_ratio == pytest.approx(paper_like_params.risk_reward_ratio)
    assert len(executor.calls) == 1


def test_small_account_mode_keeps_stake_when_above_minimum() -> None:
    settings.small_account_mode = True
    exchange = _FakeExchangeWithFilters(min_notional=5.0, min_qty=0.00001, step_size=0.00001)
    executor = _FakeOrderExecutor(exchange=exchange)
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=_FixedRiskManager(stake_amount=8.0, quantity=0.0002),
        portfolio_value_provider=_StaticPortfolioProvider("20"),
    )

    result = service.execute_market_buy_with_risk(
        trade=_FakeTrade(),
        symbol="BTC/USDT",
        entry_price=40000.0,
        stop_loss=39000.0,
        take_profit=42000.0,
    )

    assert result.risk_params.stake_amount == pytest.approx(8.0)
    assert result.risk_params.quantity == pytest.approx(0.0002)
    assert len(executor.calls) == 1
    assert executor.calls[0]["quantity"] == pytest.approx(0.0002)


def test_small_account_mode_adjusts_stake_to_min_notional() -> None:
    settings.small_account_mode = True
    exchange = _FakeExchangeWithFilters(min_notional=5.0, min_qty=0.00001, step_size=0.00001)
    executor = _FakeOrderExecutor(exchange=exchange)
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=_FixedRiskManager(stake_amount=0.32, quantity=0.000005),
        portfolio_value_provider=_StaticPortfolioProvider("20"),
    )

    result = service.execute_market_buy_with_risk(
        trade=_FakeTrade(),
        symbol="BTC/USDT",
        entry_price=63000.0,
        stop_loss=62000.0,
        take_profit=65000.0,
    )

    expected_min = 5.0 * (1.0 + max(0.01, float(settings.trading.min_notional_buffer_pct)))
    assert result.risk_params.stake_amount >= expected_min
    assert result.risk_params.quantity >= 0.00001
    assert len(executor.calls) == 1
    assert float(executor.calls[0]["quantity"]) >= 0.00001


def test_small_account_mode_does_not_send_order_when_free_usdt_below_minimum() -> None:
    settings.small_account_mode = True
    exchange = _FakeExchangeWithFilters(min_notional=5.0, min_qty=0.00001, step_size=0.00001)
    executor = _FakeOrderExecutor(exchange=exchange)
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=_FixedRiskManager(stake_amount=0.32, quantity=0.000005),
        portfolio_value_provider=_StaticPortfolioProvider("3"),
    )

    with pytest.raises(ValueError, match="SMALL_ACCOUNT_MODE minimum stake"):
        service.execute_market_buy_with_risk(
            trade=_FakeTrade(),
            symbol="BTC/USDT",
            entry_price=63000.0,
            stop_loss=62000.0,
            take_profit=65000.0,
        )

    assert executor.calls == []


def test_small_account_mode_disabled_keeps_current_behavior() -> None:
    settings.small_account_mode = False
    exchange = _FakeExchangeWithFilters(min_notional=5.0, min_qty=0.00001, step_size=0.00001)
    executor = _FakeOrderExecutor(exchange=exchange)
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=_FixedRiskManager(stake_amount=0.32, quantity=0.000005),
        portfolio_value_provider=_StaticPortfolioProvider("20"),
    )

    result = service.execute_market_buy_with_risk(
        trade=_FakeTrade(),
        symbol="BTC/USDT",
        entry_price=63000.0,
        stop_loss=62000.0,
        take_profit=65000.0,
    )

    assert result.risk_params.stake_amount == pytest.approx(0.32)
    assert result.risk_params.quantity == pytest.approx(0.000005)
    assert len(exchange.calls) == 0


def test_small_account_mode_preserves_minimum_free_reserve() -> None:
    settings.small_account_mode = True
    exchange = _FakeExchangeWithFilters(min_notional=5.0, min_qty=0.00001, step_size=0.00001)
    executor = _FakeOrderExecutor(exchange=exchange)
    service = LiveRiskService(
        order_executor=executor,
        risk_manager=_FixedRiskManager(stake_amount=0.20, quantity=0.000005),
        portfolio_value_provider=_StaticPortfolioProvider("10"),
    )

    with pytest.raises(ValueError, match="reserve protection"):
        service.execute_market_buy_with_risk(
            trade=_FakeTrade(),
            symbol="BTC/USDT",
            entry_price=63000.0,
            stop_loss=62000.0,
            take_profit=65000.0,
        )

    assert executor.calls == []
