"""
Abstract base class for all trading strategies.

Design decision: Enforcing a fixed interface (initialize / calculate /
entry_signal / exit_signal / score) means every strategy is interchangeable
within the backtesting engine and paper/live trader without any conditional
logic in the caller.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd


class SignalType(str, Enum):
    """Possible signal values a strategy can emit."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategySignal:
    """
    Encapsulates a strategy's output for a single evaluation.

    Attributes:
        signal: BUY, SELL, or HOLD.
        price: Reference price at the moment the signal is generated.
        timestamp: UTC time of the signal.
        score: Numeric confidence/strength value in [0, 1].
        stop_loss: Suggested stop-loss price (absolute).
        take_profit: Suggested take-profit price (absolute).
        trailing_stop_pct: Optional trailing stop as a fraction of price.
        metadata: Arbitrary extra data for logging/debugging.
    """

    signal: SignalType
    price: float
    timestamp: datetime
    score: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Interface all strategy implementations must satisfy.

    Lifecycle::

        strategy.initialize()
        for each candle batch:
            strategy.calculate(df)
            signal = strategy.entry_signal(df)
            if open trade:
                signal = strategy.exit_signal(df, entry_price)

    Subclasses define their own indicator parameters in ``__init__``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier used in logs and database records."""

    @abstractmethod
    def initialize(self) -> None:
        """
        One-time setup: instantiate indicators, load any required state.

        Called once before the strategy starts processing data.
        """

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Enrich *df* with all indicator columns required by this strategy.

        Args:
            df: Raw OHLCV DataFrame.

        Returns:
            New DataFrame (a copy of *df*) with added indicator columns.
            Must not mutate the input.
        """

    @abstractmethod
    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        """
        Evaluate whether to open a new position based on the enriched *df*.

        Args:
            df: Output of ``calculate()``.

        Returns:
            StrategySignal with BUY, SELL (for short), or HOLD.
        """

    @abstractmethod
    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        """
        Evaluate whether to close an existing position.

        Args:
            df: Output of ``calculate()``.
            entry_price: Price at which the position was opened.

        Returns:
            StrategySignal with SELL (close long), BUY (close short), or HOLD.
        """

    @abstractmethod
    def score(self, df: pd.DataFrame) -> float:
        """
        Return a confidence score in [0, 1] for the current setup.

        Higher values indicate stronger conviction.  Scores are used for
        position sizing and trade filtering.

        Args:
            df: Output of ``calculate()``.

        Returns:
            Float in [0, 1].
        """

    def prepare_dataset(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Pre-compute and cache all features required by this strategy for *df*.

        The default implementation computes the enriched dataset once and reuses
        it while the dataset and strategy parameters remain unchanged.
        """

        cache_key = self._build_dataset_cache_key(df, symbol=symbol, timeframe=timeframe)
        cached_key = getattr(self, "_prepared_dataset_cache_key", None)
        cached_df = getattr(self, "_prepared_dataset_cache", None)

        if cached_key == cache_key and cached_df is not None:
            return cached_df

        prepared = self.calculate(df)
        self._prepared_dataset_cache_key = cache_key
        self._prepared_dataset_cache = prepared
        self._prepared_dataset_aux_cache = {}
        return prepared

    def invalidate_prepared_dataset(self) -> None:
        """Clear cached prepared dataset and any auxiliary execution caches."""

        self._prepared_dataset_cache_key = None
        self._prepared_dataset_cache = None
        self._prepared_dataset_aux_cache = {}

    def cache_payload(self, name: str, payload: Any) -> None:
        """Store a reusable execution payload tied to the current dataset cache."""

        aux = getattr(self, "_prepared_dataset_aux_cache", None)
        if aux is None:
            aux = {}
            self._prepared_dataset_aux_cache = aux
        aux[str(name)] = payload

    def cached_payload(self, name: str, default: Any = None) -> Any:
        """Read a reusable execution payload tied to the current dataset cache."""

        aux = getattr(self, "_prepared_dataset_aux_cache", None)
        if aux is None:
            return default
        return aux.get(str(name), default)

    def execution_cache_signature(self) -> tuple[tuple[str, Any], ...]:
        """
        Return a stable signature of scalar strategy parameters for cache invalidation.
        """

        signature: list[tuple[str, Any]] = []
        for key, value in sorted(self.__dict__.items()):
            if "cache" in key:
                continue
            if isinstance(value, (int, float, str, bool, type(None))):
                signature.append((key, value))
            elif isinstance(value, Path):
                signature.append((key, str(value)))
        return tuple(signature)

    def _build_dataset_cache_key(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None,
        timeframe: str | None,
    ) -> tuple[Any, ...]:
        first_idx = None if df.empty else str(df.index[0])
        last_idx = None if df.empty else str(df.index[-1])
        return (
            self.__class__.__name__,
            symbol,
            timeframe,
            len(df),
            tuple(str(col) for col in df.columns),
            first_idx,
            last_idx,
            self.execution_cache_signature(),
        )
