"""
Average True Range (ATR) indicator.
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class ATR(BaseIndicator):
    """
    Average True Range - measures market volatility.

    Commonly used to:
    - Set dynamic stop-loss distances.
    - Size positions proportionally to current volatility.

    Args:
        period: Smoothing period (default 14).
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError(f"ATR period must be >= 1, got {period}.")
        self._period = period

    @property
    def name(self) -> str:
        return f"atr_{self._period}"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute ATR using Wilder's smoothing method.

        True Range = max(high - low, |high - prev_close|, |low - prev_close|)

        Args:
            df: OHLCV DataFrame (must contain high, low, close).

        Returns:
            Series named ``atr_<period>`` with ATR values.
        """
        self._validate_min_length(df, self._period + 1)

        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.ewm(com=self._period - 1, adjust=False).mean()
        atr.name = self.name
        return atr
