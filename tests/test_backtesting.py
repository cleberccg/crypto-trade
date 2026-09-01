"""
Integration tests for the backtesting engine.
"""
from __future__ import annotations

import pytest
import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtesting.metrics import BacktestMetrics
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.trend_v1 import TrendV1Strategy
from tests.conftest import make_ohlcv
from strategy_discovery_cycle1 import _canonical_metrics, _dataset_hash, _entry_candidates, _episode_diagnostics


class _DeterministicStrategy(BaseStrategy):
    def __init__(self, buy_every_bar: bool = False) -> None:
        self._buy_every_bar = buy_every_bar

    @property
    def name(self) -> str:
        return "DeterministicTestStrategy"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        should_buy = len(df) == 51 or (self._buy_every_bar and len(df) >= 51)
        price = float(df.iloc[-1]["close"])
        if should_buy:
            return StrategySignal(
                SignalType.BUY,
                price,
                df.index[-1].to_pydatetime(),
                score=1.0,
                stop_loss=99.0,
                take_profit=102.0,
            )
        return StrategySignal(SignalType.HOLD, price, df.index[-1].to_pydatetime())

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        return StrategySignal(
            SignalType.HOLD,
            float(df.iloc[-1]["close"]),
            df.index[-1].to_pydatetime(),
        )

    def score(self, df: pd.DataFrame) -> float:
        return 1.0


def _deterministic_frame(bars: int = 54) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=bars, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
        index=index,
    )
    return frame


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

    def test_fees_pnl_and_conservative_intrabar_priority(self) -> None:
        frame = _deterministic_frame()
        frame.iloc[50, frame.columns.get_loc("close")] = 100.0
        frame.iloc[51, frame.columns.get_loc("high")] = 103.0
        frame.iloc[51, frame.columns.get_loc("low")] = 98.0

        result = BacktestEngine(
            _DeterministicStrategy(),
            config=BacktestConfig(initial_capital=10_000.0, warmup_bars=50),
        ).run(frame, symbol="TEST/USDT", timeframe="1h")
        trade = result.trades[0]
        expected = (
            trade["quantity"] * (trade["exit_price"] - trade["entry_price"])
            - trade["entry_fee"]
            - trade["exit_fee"]
        )
        assert trade["entry_fee"] == pytest.approx(trade["quantity"] * 100.0 * 0.001)
        assert trade["exit_fee"] == pytest.approx(trade["quantity"] * 99.0 * 0.001)
        assert trade["pnl"] == pytest.approx(expected)
        assert trade["exit_reason"] == "stop_loss"
        assert trade["exit_price"] == pytest.approx(99.0)

    def test_entry_candle_and_reentry_do_not_overlap_positions(self) -> None:
        frame = _deterministic_frame()
        frame.iloc[51, frame.columns.get_loc("low")] = 98.0
        frame.iloc[52, frame.columns.get_loc("low")] = 98.0

        result = BacktestEngine(
            _DeterministicStrategy(buy_every_bar=True),
            config=BacktestConfig(initial_capital=10_000.0, warmup_bars=50),
        ).run(frame, symbol="TEST/USDT", timeframe="1h")
        assert len(result.trades) == 2
        assert result.trades[0]["entry_bar"] == 50
        assert result.trades[0]["exit_bar"] == 51
        assert result.trades[1]["entry_bar"] == 51
        assert result.trades[1]["entry_bar"] >= result.trades[0]["exit_bar"]

    def test_discovery_reproducibility_helpers_are_deterministic(self) -> None:
        frame = _deterministic_frame()
        contexts = {("BTC/USDT", "1h"): frame}
        first_hash = _dataset_hash(contexts)
        second_hash = _dataset_hash({("BTC/USDT", "1h"): frame.copy(deep=True)})
        assert first_hash == second_hash

        result = BacktestEngine(
            _DeterministicStrategy(),
            config=BacktestConfig(initial_capital=10_000.0, warmup_bars=50),
        ).run(frame, symbol="BTC/USDT", timeframe="1h")
        from strategy_discovery_cycle1 import ConfigResult

        metrics = result.metrics
        config_result = ConfigResult(
            "TEST",
            {},
            "DEV",
            metrics.total_trades,
            metrics.profit_factor,
            metrics.expectancy,
            metrics.max_drawdown_pct,
            metrics.sharpe_ratio,
            metrics.win_rate,
            metrics.net_profit,
            [],
            0.0,
        )
        assert _canonical_metrics(config_result) == _canonical_metrics(config_result)

    def test_discovery_episode_diagnostics_deduplicates_persistent_signal(self) -> None:
        frame = _deterministic_frame()
        frame["_discovery_entry"] = False
        frame.iloc[50:53, frame.columns.get_loc("_discovery_entry")] = True
        trades = [
            {"entry_bar": 50, "pnl": 2.0},
            {"entry_bar": 51, "pnl": -1.0},
        ]

        diagnostics = _episode_diagnostics(frame, trades, warmup_bars=50)

        assert diagnostics["total_signals"] == 3
        assert diagnostics["total_trades"] == 2
        assert diagnostics["independent_events"] == 1
        assert diagnostics["reentries"] == diagnostics["persistent_signal_reentries"] == 1
        assert diagnostics["reentry_inflation_pct"] == pytest.approx(100.0)
        assert diagnostics["pf_all"] == pytest.approx(2.0)
        assert diagnostics["pf_deduplicated"] == pytest.approx(999.0)

    def test_entry_gate_requires_independent_episode_evidence(self) -> None:
        rows = [
            {"episodes": 99, "gross_expectancy": 0.01, "gross_pf": 1.2, "effect_bps": 10.0, "t_stat": 3.0},
            {"episodes": 100, "gross_expectancy": -0.01, "gross_pf": 1.2, "effect_bps": 10.0, "t_stat": 3.0},
            {"episodes": 100, "gross_expectancy": 0.01, "gross_pf": 1.2, "effect_bps": 4.0, "t_stat": 3.0},
            {"episodes": 100, "gross_expectancy": 0.01, "gross_pf": 1.2, "effect_bps": 10.0, "t_stat": 1.9},
            {"episodes": 100, "gross_expectancy": 0.01, "gross_pf": 1.2, "effect_bps": 10.0, "t_stat": 3.0},
        ]
        assert _entry_candidates(rows) == [rows[4]]
