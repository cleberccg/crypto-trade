"""
Shared pytest fixtures used across all test modules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.trend_v1 import TrendV1Strategy


# ---------------------------------------------------------------------------
# OHLCV data factory
# ---------------------------------------------------------------------------


def make_ohlcv(
    n: int = 200,
    base_price: float = 40_000.0,
    trend: float = 0.0001,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Generate synthetic OHLCV data for testing.

    Args:
        n: Number of bars.
        base_price: Starting close price.
        trend: Per-bar multiplicative drift.
        seed: Random seed for reproducibility.

    Returns:
        Normalised OHLCV DataFrame with a UTC DatetimeIndex.
    """
    rng = np.random.default_rng(seed)
    prices = [base_price]
    for _ in range(n - 1):
        change = rng.normal(trend, 0.002)
        prices.append(prices[-1] * (1 + change))

    closes = np.array(prices)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes * (1 + rng.uniform(0.001, 0.005, n))
    lows = closes * (1 - rng.uniform(0.001, 0.005, n))
    volumes = rng.uniform(1_000, 10_000, n)

    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    index = pd.date_range(start=start, periods=n, freq="1h", tz="UTC")

    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """Standard 200-bar OHLCV DataFrame."""
    return make_ohlcv(n=200)


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    """OHLCV DataFrame with a clear uptrend."""
    return make_ohlcv(n=200, trend=0.003)


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    """OHLCV DataFrame with a clear downtrend."""
    return make_ohlcv(n=200, trend=-0.003)


@pytest.fixture
def trend_v1() -> TrendV1Strategy:
    """Initialised TrendV1 strategy."""
    strategy = TrendV1Strategy()
    strategy.initialize()
    return strategy
