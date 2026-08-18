from __future__ import annotations

from abc import abstractmethod

from strategies.base_strategy import BaseStrategy


class QuantStrategy(BaseStrategy):
    @property
    @abstractmethod
    def family(self) -> str:
        """Strategy family used by the registry and cross-family reports."""


class TrendStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "trend"


class MeanReversionStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "mean_reversion"


class BreakoutStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "breakout"


class MomentumStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "momentum"


class RangeStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "range"


class VWAPStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "vwap"


class OpeningRangeStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "opening_range"


class LiquiditySweepStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "liquidity_sweep"


class MarketStructureStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "market_structure"


class ReversalEdgeStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "reversal_edge"
