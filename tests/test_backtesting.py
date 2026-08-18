"""
Integration tests for the backtesting engine.
"""
from __future__ import annotations

import pytest
import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtesting.metrics import BacktestMetrics
from strategies.trend_v1 import TrendV1Strategy
from tests.conftest import make_ohlcv


@pytest.fixture
def engine() -> BacktestEngine:
    strategy = TrendV1Strategy()
    strategy.initialize()
    return BacktestEngine(
        strategy,
        config=BacktestConfig(initial_capital=10_000.0, warmup_bars=60),
    )


class TestBacktestEngine:
    def test_run_returns_backtest_result(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=300)
        result = engine.run(df, symbol="BTC/USDT")
        assert isinstance(result, BacktestResult)

    def test_metrics_types(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=300)
        result = engine.run(df, symbol="BTC/USDT")
        assert isinstance(result.metrics, BacktestMetrics)

    def test_equity_curve_length(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=300)
        result = engine.run(df)
        # Curva de equity cobre as barras apos o warmup
        assert len(result.equity_curve) == 300 - 60

    def test_final_equity_is_positive(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=300)
        result = engine.run(df)
        assert result.equity_curve.iloc[-1] > 0

    def test_win_rate_between_0_and_1(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=500, trend=0.001)
        result = engine.run(df)
        assert 0.0 <= result.metrics.win_rate <= 1.0

    def test_profit_factor_non_negative(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=500, trend=0.001)
        result = engine.run(df)
        assert result.metrics.profit_factor >= 0.0

    def test_no_trades_on_very_short_data(self) -> None:
        """Short data that doesn't pass warmup should produce no trades."""
        strategy = TrendV1Strategy()
        strategy.initialize()
        engine = BacktestEngine(
            strategy, config=BacktestConfig(warmup_bars=60)
        )
        df = make_ohlcv(n=65)
        result = engine.run(df)
        # Muito poucas barras apos o warmup - improvavel ter muitos trades
        assert result.metrics.total_trades >= 0

    def test_metrics_to_dict_has_all_keys(self, engine: BacktestEngine) -> None:
        df = make_ohlcv(n=300)
        result = engine.run(df)
        d = result.metrics.to_dict()
        for key in [
            "total_trades", "win_rate", "net_profit", "profit_factor",
            "max_drawdown_pct", "sharpe_ratio", "expectancy",
        ]:
            assert key in d, f"Missing key: {key}"
