"""
Bollinger Bands indicator.
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class BollingerBands(BaseIndicator):
    """
    Bollinger Bands - volatility envelope around a Simple Moving Average.

    Bands:
    - ``middle``: n-period SMA of close.
    - ``upper``: middle + (k * std).
    - ``lower``: middle - (k * std).
    - ``bandwidth``: (upper - lower) / middle - normalised width.
    - ``percent_b``: position of close within the bands (0 = lower, 1 = upper).

    Args:
        period: Lookback period for the SMA (default 20).
        std_dev: Standard deviation multiplier (default 2.0).
    """

    def __init__(self, period: int = 20, std_dev: float = 2.0) -> None:
        if period < 2:
            raise ValueError(f"BollingerBands period must be >= 2, got {period}.")
        if std_dev <= 0:
            raise ValueError(f"std_dev must be positive, got {std_dev}.")
        self._period = period
        self._std_dev = std_dev

    @property
    def name(self) -> str:
        return f"bb_{self._period}_{self._std_dev}"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute Bollinger Band components.

        Args:
            df: OHLCV DataFrame.

        Returns:
            DataFrame with columns [middle, upper, lower, bandwidth, percent_b].
        """
        self._validate_min_length(df, self._period)

        close = df["close"]
        middle = close.rolling(window=self._period).mean()
        std = close.rolling(window=self._period).std(ddof=0)

        upper = middle + self._std_dev * std
        lower = middle - self._std_dev * std
        bandwidth = (upper - lower) / middle
        percent_b = (close - lower) / (upper - lower)

        return pd.DataFrame(
            {
                "middle": middle,
                "upper": upper,
                "lower": lower,
                "bandwidth": bandwidth,
                "percent_b": percent_b,
            },
            index=df.index,
        )
