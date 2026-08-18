"""
Unit tests for risk management components.
"""
from __future__ import annotations

import pytest

from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager


class TestPositionSizer:
    def test_fixed_fractional_basic(self) -> None:
        sizer = PositionSizer()
        qty = sizer.fixed_fractional(
            portfolio_value=10_000.0,
            stake_pct=0.02,
            price=40_000.0,
        )
        # 10000 * 0.02 / 40000 = 0.005
        assert abs(qty - 0.005) < 1e-8

    def test_fixed_fractional_invalid_portfolio(self) -> None:
        sizer = PositionSizer()
        with pytest.raises((TypeError, ValueError)):
            sizer.fixed_fractional(
                portfolio_value=-1000.0, stake_pct=0.02, price=40_000.0
            )

    def test_fixed_fractional_stake_pct_over_1_raises(self) -> None:
        sizer = PositionSizer()
        with pytest.raises(ValueError):
            sizer.fixed_fractional(
                portfolio_value=10_000.0, stake_pct=1.5, price=40_000.0
            )

    def test_risk_based_basic(self) -> None:
        sizer = PositionSizer()
        qty = sizer.risk_based(
            portfolio_value=10_000.0,
            risk_pct=0.01,
            entry_price=100.0,
            stop_loss_price=98.0,
        )
        # max_loss = 100, risk_per_unit = 2, qty = 50
        assert abs(qty - 50.0) < 1e-8

    def test_risk_based_stop_above_entry_raises(self) -> None:
        sizer = PositionSizer()
        with pytest.raises(ValueError):
            sizer.risk_based(
                portfolio_value=10_000.0,
                risk_pct=0.01,
                entry_price=100.0,
                stop_loss_price=105.0,  # stop acima da entrada - invalido
            )


class TestRiskManager:
    def test_evaluate_trade_returns_params(self) -> None:
        rm = RiskManager()
        params = rm.evaluate_trade(
            portfolio_value=10_000.0,
            entry_price=40_000.0,
            stop_loss=39_200.0,
            take_profit=41_600.0,
        )
        assert params.quantity > 0
        assert params.stop_loss < params.stake_amount / params.quantity + 40_000.0
        assert params.risk_reward_ratio > 0

    def test_stop_loss_above_entry_raises(self) -> None:
        rm = RiskManager()
        with pytest.raises(ValueError):
            rm.evaluate_trade(
                portfolio_value=10_000.0,
                entry_price=40_000.0,
                stop_loss=41_000.0,  # acima da entrada - invalido
                take_profit=42_000.0,
            )

    def test_take_profit_below_entry_raises(self) -> None:
        rm = RiskManager()
        with pytest.raises(ValueError):
            rm.evaluate_trade(
                portfolio_value=10_000.0,
                entry_price=40_000.0,
                stop_loss=39_000.0,
                take_profit=39_500.0,  # abaixo da entrada - invalido
            )

    def test_trailing_stop_triggered(self) -> None:
        rm = RiskManager()
        # Entrada 100, maxima 110, trailing 1.5% -> nivel = 110 * 0.985 = 108.35
        # atual 108.0 -> acionado
        assert rm.check_trailing_stop(
            entry_price=100.0,
            current_price=108.0,
            highest_price=110.0,
            trailing_stop_pct=0.015,
        )

    def test_trailing_stop_not_triggered(self) -> None:
        rm = RiskManager()
        # Entrada 100, maxima 110, trailing 1.5% -> nivel = 108.35
        # atual 109.0 -> nao acionado
        assert not rm.check_trailing_stop(
            entry_price=100.0,
            current_price=109.0,
            highest_price=110.0,
            trailing_stop_pct=0.015,
        )
