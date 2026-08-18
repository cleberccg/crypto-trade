"""SuperTrend V1 strategy implementation for controlled FASE 12 validation."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from strategies.base_strategy import SignalType, StrategySignal
from strategies.families import QuantStrategy
from strategies.registry import register_strategy


def _ts(value: object) -> datetime:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return datetime.utcnow()
    return ts.to_pydatetime()


@register_strategy(
    name="SuperTrendV1",
    version="v1",
    family="crypto_catalog",
    description="SuperTrend breakout-follow strategy for crypto with ATR-based risk controls.",
    parameters=[
        "atr_period",
        "atr_multiplier",
        "trend_confirmation",
        "stop_atr_multiplier",
        "take_profit_pct",
        "risk_reward_ratio",
        "score_min",
    ],
    indicators=["ATR", "SuperTrend"],
    categories=["crypto", "trend", "volatility"],
    compatibility=["optimizer", "validation", "execution_manager", "database", "paper_trading"],
    aliases=["SuperTrend", "supertrend_v1", "supertrend"],
    parameter_aliases={
        "atr_stop_multiplier": "stop_atr_multiplier",
        "rr": "risk_reward_ratio",
    },
)
class SuperTrendV1Strategy(QuantStrategy):
    def __init__(
        self,
        atr_period: int = 10,
        atr_multiplier: float = 3.0,
        trend_confirmation: int = 1,
        stop_atr_multiplier: float = 2.0,
        take_profit_pct: float = 0.0,
        risk_reward_ratio: float = 2.0,
        score_min: float = 0.0,
        **_: object,
    ) -> None:
        self._atr_period = max(2, int(atr_period))
        self._atr_multiplier = max(0.5, float(atr_multiplier))
        self._trend_confirmation = max(1, int(trend_confirmation))
        self._stop_atr_multiplier = max(0.5, float(stop_atr_multiplier))
        self._take_profit_pct = max(0.0, float(take_profit_pct))
        self._risk_reward_ratio = max(1.0, float(risk_reward_ratio))
        self._score_min = max(0.0, float(score_min))
        self._atr: ATR | None = None

    @property
    def name(self) -> str:
        return "SuperTrendV1"

    @property
    def family(self) -> str:
        return "trend"

    def initialize(self) -> None:
        self._atr = ATR(period=self._atr_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = self._atr.calculate(out)  # type: ignore[union-attr]
        hl2 = (out["high"] + out["low"]) / 2.0
        basic_upper = hl2 + self._atr_multiplier * out["atr"]
        basic_lower = hl2 - self._atr_multiplier * out["atr"]

        final_upper = basic_upper.copy()
        final_lower = basic_lower.copy()

        for i in range(1, len(out)):
            prev_i = i - 1
            close_prev = float(out["close"].iloc[prev_i])

            if float(basic_upper.iloc[i]) < float(final_upper.iloc[prev_i]) or close_prev > float(final_upper.iloc[prev_i]):
                final_upper.iloc[i] = basic_upper.iloc[i]
            else:
                final_upper.iloc[i] = final_upper.iloc[prev_i]

            if float(basic_lower.iloc[i]) > float(final_lower.iloc[prev_i]) or close_prev < float(final_lower.iloc[prev_i]):
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                final_lower.iloc[i] = final_lower.iloc[prev_i]

        supertrend = pd.Series(index=out.index, dtype="float64")
        trend = pd.Series(index=out.index, dtype="int64")

        if len(out) > 0:
            supertrend.iloc[0] = float(final_lower.iloc[0])
            trend.iloc[0] = 1

        for i in range(1, len(out)):
            prev_i = i - 1
            close_now = float(out["close"].iloc[i])
            prev_st = float(supertrend.iloc[prev_i])
            prev_upper = float(final_upper.iloc[prev_i])

            if abs(prev_st - prev_upper) < 1e-12:
                if close_now <= float(final_upper.iloc[i]):
                    supertrend.iloc[i] = float(final_upper.iloc[i])
                    trend.iloc[i] = -1
                else:
                    supertrend.iloc[i] = float(final_lower.iloc[i])
                    trend.iloc[i] = 1
            else:
                if close_now >= float(final_lower.iloc[i]):
                    supertrend.iloc[i] = float(final_lower.iloc[i])
                    trend.iloc[i] = 1
                else:
                    supertrend.iloc[i] = float(final_upper.iloc[i])
                    trend.iloc[i] = -1

        out["supertrend"] = supertrend
        out["trend_direction"] = trend.fillna(0).astype(int)
        out["supertrend_upper"] = final_upper
        out["supertrend_lower"] = final_lower
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        timestamp = _ts(last.name)
        price = float(last["close"])
        atr = float(last.get("atr", 0.0))
        trend = int(last.get("trend_direction", 0))
        prev_trend = int(df.iloc[-2].get("trend_direction", 0)) if len(df) >= 2 else trend

        recent = df["trend_direction"].tail(self._trend_confirmation)
        trend_confirmed = bool((recent == 1).all()) if len(recent) >= self._trend_confirmation else False
        flipped_up = prev_trend != 1 and trend == 1

        confidence = self.score(df)
        if not (trend_confirmed and flipped_up and confidence * 100.0 >= self._score_min):
            return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=timestamp, score=confidence)

        stop_loss = price - self._stop_atr_multiplier * max(atr, 1e-9)
        risk = max(price - stop_loss, 1e-9)
        if self._take_profit_pct > 0:
            take_profit = price * (1.0 + self._take_profit_pct)
        else:
            take_profit = price + risk * self._risk_reward_ratio

        return StrategySignal(
            signal=SignalType.BUY,
            price=price,
            timestamp=timestamp,
            score=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_pct=settings.risk.default_trailing_stop_pct,
            metadata={
                "strategy": self.name,
                "atr_period": self._atr_period,
                "atr_multiplier": self._atr_multiplier,
                "trend_confirmation": self._trend_confirmation,
                "stop_atr_multiplier": self._stop_atr_multiplier,
                "risk_reward_ratio": self._risk_reward_ratio,
            },
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        timestamp = _ts(last.name)
        price = float(last["close"])
        trend = int(last.get("trend_direction", 0))
        if trend == -1:
            return StrategySignal(
                signal=SignalType.SELL,
                price=price,
                timestamp=timestamp,
                score=1.0,
                metadata={"exit_reason": "supertrend_flip_down", "entry_price": float(entry_price)},
            )
        return StrategySignal(
            signal=SignalType.HOLD,
            price=price,
            timestamp=timestamp,
            score=0.0,
            metadata={"entry_price": float(entry_price)},
        )

    def score(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        last = df.iloc[-1]
        price = float(last.get("close", 0.0))
        st = float(last.get("supertrend", price))
        trend = int(last.get("trend_direction", 0))
        if price <= 0:
            return 0.0
        distance = max(-0.05, min(0.05, (price - st) / price))
        base = 0.5 + distance * 8.0
        if trend < 0:
            base *= 0.5
        return max(0.0, min(1.0, base))
