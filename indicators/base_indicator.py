"""
Abstract base class for all technical indicators.

Design decision: An ABC enforces a uniform interface so strategies can work
with any indicator interchangeably.  The `calculate` method takes a DataFrame
and returns a new Series, keeping indicators side-effect-free.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseIndicator(ABC):
    """
    Interface all indicator implementations must satisfy.

    Each subclass must:
    - Accept configuration via ``__init__``.
    - Implement ``calculate`` returning a named ``pd.Series`` (or a
      ``pd.DataFrame`` for multi-output indicators such as MACD/Bollinger).
    - Be stateless between calls (no stored intermediate state).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short human-readable name used for column labelling."""

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.Series | pd.DataFrame:
        """
        Compute the indicator from *df*.

        Args:
            df: OHLCV DataFrame with at minimum [open, high, low, close, volume]
                columns and a DatetimeIndex.

        Returns:
            A ``pd.Series`` (single-output) or ``pd.DataFrame`` (multi-output)
            aligned to *df*'s index.
        """

    def _validate_min_length(self, df: pd.DataFrame, min_length: int) -> None:
        """
        Raise ValueError if *df* does not have enough rows to compute the
        indicator reliably.
        """
        if len(df) < min_length:
            raise ValueError(
                f"{self.name} requires at least {min_length} rows, "
                f"got {len(df)}."
            )
