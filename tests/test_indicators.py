"""
Unit tests for all technical indicators.
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.macd import MACD
from indicators.rsi import RSI
from tests.conftest import make_ohlcv


@pytest.fixture
def df() -> pd.DataFrame:
    return make_ohlcv(n=100)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


class TestEMA:
    def test_returns_series(self, df: pd.DataFrame) -> None:
        result = EMA(20).calculate(df)
        assert isinstance(result, pd.Series)

    def test_length_matches_input(self, df: pd.DataFrame) -> None:
        result = EMA(20).calculate(df)
        assert len(result) == len(df)

    def test_name_includes_period(self) -> None:
        assert EMA(20).name == "ema_20"

    def test_no_nan_after_warmup(self, df: pd.DataFrame) -> None:
        result = EMA(20).calculate(df)
        # Apos as primeiras 20 barras, o EWM deve estar totalmente aquecido
        assert result.iloc[20:].isna().sum() == 0

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            EMA(0)

    def test_insufficient_data_raises(self) -> None:
        small_df = make_ohlcv(n=5)
        with pytest.raises(ValueError):
            EMA(20).calculate(small_df)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestRSI:
    def test_values_between_0_and_100(self, df: pd.DataFrame) -> None:
        result = RSI(14).calculate(df)
        assert result.dropna().between(0, 100).all()

    def test_name_includes_period(self) -> None:
        assert RSI(14).name == "rsi_14"

    def test_length_matches_input(self, df: pd.DataFrame) -> None:
        result = RSI(14).calculate(df)
        assert len(result) == len(df)

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            RSI(1)

    def test_all_gains_produces_100(self) -> None:
        """Strictly increasing close prices should produce RSI near 100."""
        df = make_ohlcv(n=50, trend=0.01)
        result = RSI(14).calculate(df)
        assert result.iloc[-1] > 90.0


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestATR:
    def test_returns_series(self, df: pd.DataFrame) -> None:
        result = ATR(14).calculate(df)
        assert isinstance(result, pd.Series)

    def test_all_positive(self, df: pd.DataFrame) -> None:
        result = ATR(14).calculate(df)
        assert (result.dropna() > 0).all()

    def test_name_includes_period(self) -> None:
        assert ATR(14).name == "atr_14"


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


class TestMACD:
    def test_returns_dataframe(self, df: pd.DataFrame) -> None:
        result = MACD().calculate(df)
        assert isinstance(result, pd.DataFrame)

    def test_expected_columns(self, df: pd.DataFrame) -> None:
        result = MACD().calculate(df)
        assert {"macd", "signal", "histogram"}.issubset(result.columns)

    def test_histogram_equals_macd_minus_signal(self, df: pd.DataFrame) -> None:
        result = MACD().calculate(df)
        diff = (result["macd"] - result["signal"] - result["histogram"]).abs()
        assert (diff < 1e-10).all()

    def test_fast_must_be_less_than_slow(self) -> None:
        with pytest.raises(ValueError):
            MACD(fast_period=26, slow_period=12)


# ---------------------------------------------------------------------------
# BollingerBands
# ---------------------------------------------------------------------------


class TestBollingerBands:
    def test_returns_dataframe(self, df: pd.DataFrame) -> None:
        result = BollingerBands(20).calculate(df)
        assert isinstance(result, pd.DataFrame)

    def test_expected_columns(self, df: pd.DataFrame) -> None:
        result = BollingerBands(20).calculate(df)
        expected = {"middle", "upper", "lower", "bandwidth", "percent_b"}
        assert expected.issubset(result.columns)

    def test_upper_above_lower(self, df: pd.DataFrame) -> None:
        result = BollingerBands(20).calculate(df)
        valid = result.dropna()
        assert (valid["upper"] > valid["lower"]).all()

    def test_invalid_std_dev_raises(self) -> None:
        with pytest.raises(ValueError):
            BollingerBands(20, std_dev=-1.0)
