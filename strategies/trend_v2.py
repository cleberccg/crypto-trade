"""
TrendV2 - evidence-driven trend strategy.

TrendV2 keeps the TrendV1 interface but strengthens regime selection:
- adds a third EMA to confirm a broader trend structure;
- filters out low-volatility / lateral regimes using Bollinger bandwidth and ATR;
- keeps the same risk wiring so it remains compatible with the existing engine.
"""
from __future__ import annotations

import pandas as pd

from config.settings import settings
from indicators.ema import EMA
from strategies.base_strategy import SignalType, StrategySignal
from strategies.registry import register_strategy
from strategies.trend_v1 import TrendV1Strategy
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="TrendV2",
    version="v2",
    family="trend",
    description="Trend strategy with third-EMA structure and volatility regime filters.",
    parameters=[
        "ema_fast",
        "ema_slow",
        "ema_trend",
        "rsi_period",
        "atr_period",
        "rsi_min",
        "rsi_max",
        "atr_stop_multiplier",
        "risk_reward_ratio",
        "score_min",
        "volume_multiplier_min",
    ],
    indicators=["EMA", "RSI", "MACD", "BollingerBands", "ATR"],
    categories=["trend", "momentum", "regime_filtered", "spot"],
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
    aliases=["trend_v2", "v2"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class TrendV2Strategy(TrendV1Strategy):
    """TrendV1 successor with a stronger regime filter and trend confirmation."""

    def __init__(
        self,
        ema_fast: int = 25,
        ema_slow: int = 20,
        ema_trend: int = 110,
        rsi_period: int = 14,
        atr_period: int = 14,
        rsi_min: float = 47.0,
        rsi_max: float = 70.0,
        atr_stop_multiplier: float = 2.25,
        risk_reward_ratio: float = 3.2,
        score_min: float = 80.0,
        volume_multiplier_min: float = 2.0,
    ) -> None:
        super().__init__(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=rsi_period,
            atr_period=atr_period,
            rsi_min=rsi_min,
            rsi_max=rsi_max,
            atr_stop_multiplier=atr_stop_multiplier,
            risk_reward_ratio=risk_reward_ratio,
            score_min=score_min,
            volume_multiplier_min=volume_multiplier_min,
        )
        self._ema_trend_period = ema_trend
        self._regime_lookback = 40
        self._ema_trend: EMA | None = None

    @property
    def name(self) -> str:
        return "TrendV2"

    def initialize(self) -> None:
        super().initialize()
        self._ema_trend = EMA(period=self._ema_trend_period)
        logger.info("%s - initialized with trend EMA %d.", self.name, self._ema_trend_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        result = super().calculate(df)
        if len(df) >= self._ema_trend_period:
            result[self._ema_trend.name] = self._ema_trend.calculate(df)  # type: ignore[union-attr]
        else:
            span = max(2, min(len(df), self._ema_trend_period))
            result[self._ema_trend.name] = df["close"].ewm(span=span, adjust=False).mean()
        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]
        result["bb_bandwidth"] = bb_df["bandwidth"]
        return result

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        price = float(last["close"])
        atr = float(last["atr"])
        volume = float(last["volume"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        ema_trend_col = self._ema_trend.name  # type: ignore[union-attr]

        recent = df.tail(min(len(df), self._regime_lookback)).copy()
        recent_bandwidth = pd.to_numeric(recent.get("bb_bandwidth"), errors="coerce").dropna()
        recent_atr_pct = pd.to_numeric(recent["atr"], errors="coerce").div(recent["close"]).dropna()

        bandwidth_now = float(last.get("bb_bandwidth", 0.0))
        bandwidth_median = float(recent_bandwidth.median()) if not recent_bandwidth.empty else bandwidth_now
        atr_pct_now = atr / price if price else 0.0
        atr_pct_median = float(recent_atr_pct.median()) if not recent_atr_pct.empty else atr_pct_now

        ema_gap = float(last[ema_fast_col]) - float(last[ema_slow_col])
        regime_ok = bandwidth_now >= bandwidth_median * 0.90 and atr_pct_now >= atr_pct_median * 0.90

        trend_aligned = (
            price > float(last[ema_trend_col])
            and float(last[ema_fast_col]) > float(last[ema_trend_col])
        )
        price_above_fast = price > float(last[ema_fast_col])
        rsi_ok = self._rsi_min <= float(last["rsi"]) <= self._rsi_max
        macd_positive = float(last["macd_histogram"]) > 0
        macd_increasing = float(last["macd_histogram"]) > float(prev["macd_histogram"])
        price_above_bb_mid = price > float(last["bb_middle"])
        volume_ok = volume >= self._volume_multiplier_min * float(df["volume"].mean())

        conditions_met = (
            trend_aligned
            and price_above_fast
            and rsi_ok
            and macd_positive
            and macd_increasing
            and price_above_bb_mid
            and volume_ok
        )

        if conditions_met:
            stop_loss = price - self._atr_stop_multiplier * atr
            risk = price - stop_loss
            rr_ratio = max(self._risk_reward_ratio, 2.0)
            reward = risk * rr_ratio
            take_profit = price + reward
            trailing_stop_pct = max(0.01, min(0.03, (atr / max(price, 1e-9)) * 1.5))
            confidence = self.score(df)

            if confidence * 100 < self._score_min:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    price=price,
                    timestamp=timestamp,
                    score=confidence,
                )

            logger.info(
                "%s - BUY signal price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f",
                self.name,
                price,
                atr,
                stop_loss,
                risk,
                rr_ratio,
                reward,
                take_profit,
                confidence,
            )

            return StrategySignal(
                signal=SignalType.BUY,
                price=price,
                timestamp=timestamp,
                score=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=trailing_stop_pct,
                metadata={
                    "trend_aligned": trend_aligned,
                    "regime_ok": regime_ok,
                    "bandwidth": bandwidth_now,
                    "atr_pct": atr_pct_now,
                    "regime_ok": regime_ok,
                    "rsi": float(last["rsi"]),
                    "macd_histogram": float(last["macd_histogram"]),
                    "atr": atr,
                    "risk": risk,
                    "reward": reward,
                    "rr_ratio": rr_ratio,
                },
            )

        return StrategySignal(
            signal=SignalType.HOLD,
            price=price,
            timestamp=timestamp,
            score=0.0,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        self._assert_initialized()

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        ema_trend_col = self._ema_trend.name  # type: ignore[union-attr]

        ema_bearish_cross = (
            float(last[ema_fast_col]) < float(last[ema_slow_col])
            and float(prev[ema_fast_col]) >= float(prev[ema_slow_col])
        )
        ema_trend_lost = price < float(last[ema_trend_col])
        rsi_overbought = float(last["rsi"]) > 72.0
        macd_turned_negative = (
            float(last["macd_histogram"]) < 0
            and float(prev["macd_histogram"]) >= 0
        )
        price_below_bb_lower = price < float(last["bb_lower"])
        price_below_fast = price < float(last[ema_fast_col])

        exit_reason = None
        if ema_bearish_cross:
            exit_reason = "ema_bearish_cross"
        elif ema_trend_lost and macd_turned_negative:
            exit_reason = "ema_trend_lost"
        elif rsi_overbought:
            exit_reason = "rsi_overbought"
        elif macd_turned_negative:
            exit_reason = "macd_turned_negative"
        elif price_below_bb_lower:
            exit_reason = "price_below_bb_lower"
        elif price_below_fast:
            exit_reason = "price_below_fast"

        if exit_reason:
            pnl_pct = (price - entry_price) / entry_price * 100
            logger.info(
                "%s - SELL signal reason=%s price=%.4f pnl=%.2f%%",
                self.name,
                exit_reason,
                price,
                pnl_pct,
            )
            return StrategySignal(
                signal=SignalType.SELL,
                price=price,
                timestamp=timestamp,
                score=self.score(df),
                metadata={"exit_reason": exit_reason, "pnl_pct": pnl_pct},
            )

        return StrategySignal(
            signal=SignalType.HOLD,
            price=price,
            timestamp=timestamp,
            score=0.0,
        )

    def score(self, df: pd.DataFrame) -> float:
        self._assert_initialized()
        last = df.iloc[-1]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        ema_trend_col = self._ema_trend.name  # type: ignore[union-attr]

        price = float(last["close"])
        atr = max(float(last["atr"]), 1e-9)
        ema_gap = max(float(last[ema_fast_col]) - float(last[ema_slow_col]), 0.0)
        trend_gap = max(float(last[ema_slow_col]) - float(last[ema_trend_col]), 0.0)
        trend_score = min(1.0, (ema_gap / atr) / 2.5)
        structure_score = 1.0 if price > float(last[ema_trend_col]) and price > float(last["bb_middle"]) else 0.5

        hist = float(last["macd_histogram"])
        hist_score = 0.0
        if hist > 0:
            hist_score = min(1.0, hist / (atr * 0.25))

        recent = df.tail(min(len(df), self._regime_lookback)).copy()
        recent_bandwidth = pd.to_numeric(recent.get("bb_bandwidth"), errors="coerce").dropna()
        current_bandwidth = float(last.get("bb_bandwidth", 0.0))
        bandwidth_reference = float(recent_bandwidth.median()) if not recent_bandwidth.empty else current_bandwidth
        regime_score = 0.0
        if bandwidth_reference > 0:
            regime_score = min(1.0, current_bandwidth / bandwidth_reference)

        rsi = float(last["rsi"])
        rsi_score = max(0.0, 1.0 - abs(rsi - 55.0) / 18.0)

        trend_structure_score = min(1.0, abs(trend_gap) / (atr * 3.5))

        total = (
            trend_score * 0.28
            + trend_structure_score * 0.22
            + hist_score * 0.20
            + regime_score * 0.20
            + rsi_score * 0.10
        )
        total = total * 0.9 + structure_score * 0.1
        return round(float(max(0.0, min(1.0, total))), 4)