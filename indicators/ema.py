"""
Exponential Moving Average (EMA) indicator.
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class EMA(BaseIndicator):
    """
    Exponential Moving Average.

    Args:
        period: Number of periods for the EMA calculation.
    """

    def __init__(self, period: int = 20) -> None:
        if period < 1:
            raise ValueError(f"EMA period must be >= 1, got {period}.")
        self._period = period

    @property
    def name(self) -> str:
        return f"ema_{self._period}"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute EMA on the closing price series.

        Args:
            df: OHLCV DataFrame.

        Returns:
            Series named ``ema_<period>`` containing EMA values.
        """
        self._validate_min_length(df, self._period)
        series = df["close"].ewm(span=self._period, adjust=False).mean()
        series.name = self.name
        return series
