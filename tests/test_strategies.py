"""
Unit tests for trading strategies.
"""
from __future__ import annotations

import pytest
import pandas as pd

from config.settings import settings
from strategies.base_strategy import SignalType
from strategies.classic_catalog_strategies import ClassicDonchianBreakoutStrategy
from strategies.trend_v1 import TrendV1Strategy
from tests.conftest import make_ohlcv


class TestTrendV1Strategy:
    def test_name(self) -> None:
        assert TrendV1Strategy().name == "TrendV1"

    def test_initialize(self) -> None:
        s = TrendV1Strategy()
        s.initialize()  # nao deve gerar excecao

    def test_requires_initialize_before_calculate(self) -> None:
        s = TrendV1Strategy()
        df = make_ohlcv(n=100)
        with pytest.raises(RuntimeError):
            s.calculate(df)

    def test_calculate_returns_dataframe(self, trend_v1, ohlcv_df) -> None:
        result = trend_v1.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)

    def test_calculate_adds_indicator_columns(self, trend_v1, ohlcv_df) -> None:
        result = trend_v1.calculate(ohlcv_df)
        for col in ["rsi", "macd", "macd_signal", "macd_histogram", "atr",
                    "bb_middle", "bb_upper", "bb_lower"]:
            assert col in result.columns, f"Missing column: {col}"

    def test_entry_signal_returns_valid_type(self, trend_v1, ohlcv_df) -> None:
        enriched = trend_v1.calculate(ohlcv_df)
        signal = trend_v1.entry_signal(enriched)
        assert signal.signal in SignalType.__members__.values()

    def test_exit_signal_returns_valid_type(self, trend_v1, ohlcv_df) -> None:
        enriched = trend_v1.calculate(ohlcv_df)
        signal = trend_v1.exit_signal(enriched, entry_price=40_000.0)
        assert signal.signal in SignalType.__members__.values()

    def test_score_is_between_0_and_1(self, trend_v1, ohlcv_df) -> None:
        enriched = trend_v1.calculate(ohlcv_df)
        score = trend_v1.score(enriched)
        assert 0.0 <= score <= 1.0

    def test_uptrend_generates_buy_signal(self) -> None:
        """A strong uptrend should eventually generate a BUY signal."""
        df = make_ohlcv(n=200, trend=0.004, seed=1)
        strategy = TrendV1Strategy()
        strategy.initialize()
        enriched = strategy.calculate(df)

        # Verifica as ultimas 50 barras para ao menos um sinal de BUY
        buy_found = False
        for i in range(len(enriched) - 50, len(enriched)):
            window = enriched.iloc[: i + 1]
            sig = strategy.entry_signal(window)
            if sig.signal == SignalType.BUY:
                buy_found = True
                break
        assert buy_found, "Expected at least one BUY signal in a strong uptrend."

    def test_entry_signal_includes_stop_and_tp_on_buy(self) -> None:
        """A BUY signal must include stop_loss and take_profit."""
        df = make_ohlcv(n=200, trend=0.004, seed=99)
        strategy = TrendV1Strategy()
        strategy.initialize()
        enriched = strategy.calculate(df)

        for i in range(len(enriched) - 50, len(enriched)):
            window = enriched.iloc[: i + 1]
            sig = strategy.entry_signal(window)
            if sig.signal == SignalType.BUY:
                assert sig.stop_loss is not None
                assert sig.take_profit is not None
                assert sig.stop_loss < sig.price < sig.take_profit
                return


class TestClassicDonchianBreakoutStrategy:
    def test_entry_signal_uses_configured_stop_loss_fraction(self) -> None:
        strategy = ClassicDonchianBreakoutStrategy(donchian_window=20)
        strategy.initialize()

        n = 30
        index = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        highs = [100.0] * (n - 1) + [102.0]
        lows = [99.0] * n
        closes = [100.0] * (n - 1) + [101.0]
        opens = [100.0] * n
        volumes = [1000.0] * n

        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=index,
        )

        enriched = strategy.calculate(frame)
        signal = strategy.entry_signal(enriched)

        assert signal.signal == SignalType.BUY
        assert signal.stop_loss is not None

        expected_sl = signal.price * (1.0 - settings.risk.default_stop_loss_pct)
        assert signal.stop_loss == pytest.approx(expected_sl)

    def test_entry_signal_uses_risk_reward_formula_for_take_profit(self) -> None:
        strategy = ClassicDonchianBreakoutStrategy(donchian_window=20)
        strategy.initialize()

        n = 30
        index = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        highs = [100.0] * (n - 1) + [102.0]
        lows = [99.0] * n
        closes = [100.0] * (n - 1) + [101.0]
        opens = [100.0] * n
        volumes = [1000.0] * n

        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=index,
        )

        enriched = strategy.calculate(frame)
        signal = strategy.entry_signal(enriched)

        assert signal.signal == SignalType.BUY
        assert signal.stop_loss is not None
        assert signal.take_profit is not None

        expected_sl = signal.price * (1.0 - settings.risk.default_stop_loss_pct)
        expected_tp = signal.price + (signal.price - expected_sl) * settings.risk.risk_reward_ratio
        assert signal.stop_loss == pytest.approx(expected_sl)
        assert signal.take_profit == pytest.approx(expected_tp)

    def test_default_stop_loss_percent_for_runtime_is_point_six_percent(self) -> None:
        assert settings.risk.default_stop_loss_pct == pytest.approx(0.006)
