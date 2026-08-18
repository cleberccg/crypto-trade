from __future__ import annotations

import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.rsi import RSI
from strategies.base_strategy import SignalType, StrategySignal
from strategies.families import MeanReversionStrategy
from strategies.registry import register_strategy
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="MeanReversionV1",
    version="v1",
    family="mean_reversion",
    description="Mean reversion strategy based on Bollinger deviations and RSI exhaustion.",
    parameters=[
        "ema_fast",
        "ema_slow",
        "rsi_period",
        "atr_period",
        "rsi_min",
        "rsi_max",
        "atr_stop_multiplier",
        "risk_reward_ratio",
        "score_min",
        "volume_multiplier_min",
    ],
    indicators=["EMA", "RSI", "BollingerBands", "ATR"],
    categories=["contrarian", "mean_reversion", "spot"],
    compatibility=[
        "optimizer",
        "validation",
        "research_lab",
        "trade_management_lab",
        "execution_manager",
        "database",
        "checkpoints",
        "resume",
        "recovery",
    ],
    aliases=["meanreversionv1", "mean_reversion_v1", "mrv1"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class MeanReversionV1Strategy(MeanReversionStrategy):
    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        rsi_min: float = 30.0,
        rsi_max: float = 70.0,
        atr_stop_multiplier: float = 2.0,
        risk_reward_ratio: float = 1.8,
        score_min: float = 0.0,
        volume_multiplier_min: float = 1.0,
    ) -> None:
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._rsi_min = rsi_min
        self._rsi_max = rsi_max
        self._atr_stop_multiplier = atr_stop_multiplier
        self._risk_reward_ratio = risk_reward_ratio
        self._score_min = score_min
        self._volume_multiplier_min = volume_multiplier_min

        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None
        self._rsi: RSI | None = None
        self._bb: BollingerBands | None = None
        self._atr: ATR | None = None

    @property
    def name(self) -> str:
        return "MeanReversionV1"

    def initialize(self) -> None:
        self._ema_fast = EMA(period=self._ema_fast_period)
        self._ema_slow = EMA(period=self._ema_slow_period)
        self._rsi = RSI(period=self._rsi_period)
        self._bb = BollingerBands()
        self._atr = ATR(period=self._atr_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        out = df.copy()
        out[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        out[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        out["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        out["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]

        bb = self._bb.calculate(df)  # type: ignore[union-attr]
        out["bb_middle"] = bb["middle"]
        out["bb_upper"] = bb["upper"]
        out["bb_lower"] = bb["lower"]
        out["bb_percent_b"] = bb["percent_b"]
        out["bb_bandwidth"] = bb["bandwidth"]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]
        volume = float(last["volume"])

        mean_reversion_zone = price < float(last["bb_lower"])
        rsi_exhausted = float(last["rsi"]) <= self._rsi_min
        trend_not_collapsing = float(last[self._ema_fast.name]) >= float(last[self._ema_slow.name]) * 0.97  # type: ignore[union-attr]
        volume_ok = volume >= self._volume_multiplier_min * float(df["volume"].mean())

        if mean_reversion_zone and rsi_exhausted and trend_not_collapsing and volume_ok:
            stop_loss = price - self._atr_stop_multiplier * atr
            risk = max(price - stop_loss, 1e-9)
            reward = risk * max(self._risk_reward_ratio, 1.2)
            take_profit = price + reward
            confidence = self.score(df)

            if confidence * 100 < self._score_min:
                return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=confidence)

            return StrategySignal(
                signal=SignalType.BUY,
                price=price,
                timestamp=timestamp,
                score=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=settings.risk.default_trailing_stop_pct,
                metadata={
                    "entry_reason": "bb_lower_plus_rsi_exhaustion",
                    "atr": atr,
                    "risk": risk,
                    "reward": reward,
                },
            )

        return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        hit_mean = price >= float(last["bb_middle"])
        rsi_overbought = float(last["rsi"]) >= self._rsi_max
        ema_lost = float(last[self._ema_fast.name]) < float(last[self._ema_slow.name])  # type: ignore[union-attr]

        reason = None
        if hit_mean:
            reason = "reverted_to_mean"
        elif rsi_overbought:
            reason = "rsi_overbought"
        elif ema_lost:
            reason = "ema_lost"

        if reason:
            pnl_pct = (price - entry_price) / max(entry_price, 1e-9) * 100.0
            return StrategySignal(
                signal=SignalType.SELL,
                price=price,
                timestamp=timestamp,
                score=self.score(df),
                metadata={"exit_reason": reason, "pnl_pct": pnl_pct},
            )

        return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        rsi = float(last["rsi"])
        pct_b = float(last["bb_percent_b"])

        rsi_score = max(0.0, min(1.0, (self._rsi_max - rsi) / max(self._rsi_max - self._rsi_min, 1e-9)))
        band_score = max(0.0, min(1.0, 1.0 - pct_b))
        return round(float(rsi_score * 0.6 + band_score * 0.4), 4)

    def _assert_initialized(self) -> None:
        if not all([self._ema_fast, self._ema_slow, self._rsi, self._bb, self._atr]):
            raise RuntimeError(f"{self.name} not initialized. Call initialize() first.")
