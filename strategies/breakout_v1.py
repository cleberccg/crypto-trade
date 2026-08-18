"""BreakoutV1 - first breakout family implementation for phase 2 discovery."""
from __future__ import annotations

import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.ema import EMA
from indicators.rsi import RSI
from strategies.base_strategy import SignalType, StrategySignal
from strategies.families import BreakoutStrategy
from strategies.registry import register_strategy
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="BreakoutV1",
    version="v1",
    family="breakout",
    description="Breakout strategy based on rolling highs, trend alignment, RSI and volume confirmation.",
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
        "breakout_window",
        "breakout_buffer",
    ],
    indicators=["EMA", "RSI", "ATR"],
    categories=["breakout", "momentum", "spot"],
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
    aliases=["breakout_v1", "breakoutv1", "brk1"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class BreakoutV1Strategy(BreakoutStrategy):
    """Long-only breakout strategy tuned for the phase 2 pilot."""

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        rsi_min: float = 48.0,
        rsi_max: float = 78.0,
        atr_stop_multiplier: float = 2.4,
        risk_reward_ratio: float = 2.2,
        score_min: float = 0.0,
        volume_multiplier_min: float = 1.0,
        breakout_window: int = 20,
        breakout_buffer: float = 0.0005,
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
        self._breakout_window = breakout_window
        self._breakout_buffer = breakout_buffer

        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None
        self._rsi: RSI | None = None
        self._atr: ATR | None = None

    @property
    def name(self) -> str:
        return "BreakoutV1"

    def initialize(self) -> None:
        self._ema_fast = EMA(period=self._ema_fast_period)
        self._ema_slow = EMA(period=self._ema_slow_period)
        self._rsi = RSI(period=self._rsi_period)
        self._atr = ATR(period=self._atr_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        result = df.copy()
        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]
        breakout_min_periods = max(5, self._breakout_window // 2)
        result["breakout_high"] = df["high"].rolling(self._breakout_window, min_periods=breakout_min_periods).max().shift(1)
        result["breakout_low"] = df["low"].rolling(self._breakout_window, min_periods=breakout_min_periods).min().shift(1)
        result["breakout_buffer"] = self._breakout_buffer
        return result

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        price = float(last["close"])
        atr = float(last["atr"])
        volume = float(last["volume"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        breakout_high = float(last.get("breakout_high", float("nan")))
        if pd.isna(breakout_high):
            return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=0.0)

        breakout_trigger = price > breakout_high * (1.0 + self._breakout_buffer)
        trend_aligned = float(last[self._ema_fast.name]) > float(last[self._ema_slow.name])  # type: ignore[union-attr]
        momentum_ok = self._rsi_min <= float(last["rsi"]) <= self._rsi_max
        volume_ok = volume >= self._volume_multiplier_min * float(df["volume"].mean())
        breakout_strength = price >= float(prev["close"]) or float(last[self._ema_fast.name]) >= float(prev[self._ema_fast.name])  # type: ignore[union-attr]

        if breakout_trigger and trend_aligned and momentum_ok and volume_ok and breakout_strength:
            stop_loss = price - self._atr_stop_multiplier * atr
            risk = max(price - stop_loss, 1e-9)
            reward = risk * max(self._risk_reward_ratio, 1.2)
            take_profit = price + reward
            confidence = self.score(df)

            if confidence * 100 < self._score_min:
                return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=confidence)

            logger.info(
                "%s – BUY breakout price=%.4f breakout=%.4f atr=%.4f stop=%.4f rr=%.2f score=%.2f",
                self.name,
                price,
                breakout_high,
                atr,
                stop_loss,
                self._risk_reward_ratio,
                confidence,
            )

            return StrategySignal(
                signal=SignalType.BUY,
                price=price,
                timestamp=timestamp,
                score=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=settings.risk.default_trailing_stop_pct,
                metadata={
                    "entry_reason": "breakout_above_rolling_high",
                    "breakout_high": breakout_high,
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
        breakout_high = float(last.get("breakout_high", float("nan")))

        broken_back = pd.notna(breakout_high) and price < breakout_high
        trend_lost = float(last[self._ema_fast.name]) < float(last[self._ema_slow.name])  # type: ignore[union-attr]
        rsi_exhausted = float(last["rsi"]) >= self._rsi_max

        reason = None
        if broken_back:
            reason = "failed_breakout"
        elif trend_lost:
            reason = "trend_lost"
        elif rsi_exhausted:
            reason = "rsi_exhausted"

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
        price = float(last["close"])
        breakout_high = float(last.get("breakout_high", price))
        atr = float(last.get("atr", 0.0))
        volume = float(last.get("volume", 0.0))

        extension = max(0.0, (price - breakout_high) / max(breakout_high, 1e-9))
        extension_score = max(0.0, min(1.0, extension / max(self._breakout_buffer * 4.0, 1e-6)))
        volume_score = max(0.0, min(1.0, volume / max(float(df["volume"].mean()), 1e-9)))
        trend_score = max(0.0, min(1.0, float(last[self._ema_fast.name]) / max(float(last[self._ema_slow.name]), 1e-9)))  # type: ignore[union-attr]
        atr_score = max(0.0, min(1.0, 1.0 - min(1.0, atr / max(price, 1e-9))))
        rsi_score = max(0.0, min(1.0, (self._rsi_max - float(last["rsi"])) / max(self._rsi_max - self._rsi_min, 1e-9)))
        return round(float(0.30 * extension_score + 0.25 * volume_score + 0.20 * trend_score + 0.15 * atr_score + 0.10 * rsi_score), 4)

    def _assert_initialized(self) -> None:
        if not all([self._ema_fast, self._ema_slow, self._rsi, self._atr]):
            raise RuntimeError(f"{self.name} not initialized. Call initialize() first.")