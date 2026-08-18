"""
TradeOutcomeNextGenV1.1 - first controlled operational improvement (FASE 9.4).

Entry logic is intentionally identical to V1.0.
Only exit management changes are applied, starting with a time stop based on
FASE 9.3 audit recommendation.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from config.settings import settings
from indicators.ema import EMA
from strategies.base_strategy import SignalType, StrategySignal
from strategies.families import QuantStrategy
from strategies.registry import register_strategy


@register_strategy(
    name="TradeOutcomeNextGenV1.1",
    version="v1.1",
    family="trade_outcome",
    description="FASE 9.4 controlled improvement: same entry as V1.0 + time stop exit management.",
    parameters=["ema_slow", "distance_threshold", "time_stop_hours"],
    indicators=["EMA"],
    categories=["trade_outcome", "long", "rule_based", "controlled_improvement"],
    compatibility=[
        "optimizer",
        "validation",
        "execution_manager",
        "database",
        "checkpoints",
        "resume",
        "recovery",
    ],
    aliases=["trade_outcome_v1_1", "tonextgenv11", "TradeOutcomeNextGenV11"],
)
class TradeOutcomeNextGenV11Strategy(QuantStrategy):
    """V1.1 keeps entry logic unchanged and adds a deterministic time stop."""

    def __init__(
        self,
        ema_slow: int = 50,
        distance_threshold: float = 0.162026,
        time_stop_hours: float = 6.0,
        **_: object,
    ) -> None:
        self._ema_slow_period = int(ema_slow)
        self._distance_threshold = float(distance_threshold)
        self._time_stop_hours = float(max(0.5, time_stop_hours))
        self._time_stop_minutes = int(round(self._time_stop_hours * 60.0))
        self._ema_slow: EMA | None = None

    @property
    def name(self) -> str:
        return "TradeOutcomeNextGenV1.1"

    @property
    def family(self) -> str:
        return "trade_outcome"

    def initialize(self) -> None:
        self._ema_slow = EMA(period=self._ema_slow_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        out = df.copy()
        ema_col = self._ema_slow.name  # type: ignore[union-attr]

        if "close" not in out.columns:
            raise ValueError("Input dataframe must contain 'close' column.")

        out[ema_col] = self._ema_slow.calculate(out)  # type: ignore[union-attr]
        denom = out[ema_col].replace(0.0, pd.NA)
        out["distance_to_ema_pct"] = ((out["close"].astype(float) - out[ema_col].astype(float)) / denom) * 100.0
        out["distance_to_ema_pct"] = pd.to_numeric(out["distance_to_ema_pct"], errors="coerce").fillna(0.0)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()
        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = self._timestamp_from_index(last.name)
        distance = float(pd.to_numeric(last.get("distance_to_ema_pct"), errors="coerce") or 0.0)

        if distance <= self._distance_threshold:
            stop_loss = price * (1.0 - settings.risk.default_stop_loss_pct)
            risk = max(price - stop_loss, 1e-9)
            take_profit = price + (risk * settings.risk.risk_reward_ratio)
            return StrategySignal(
                signal=SignalType.BUY,
                price=price,
                timestamp=timestamp,
                score=self.score(df),
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=settings.risk.default_trailing_stop_pct,
                metadata={
                    "rule": "distance_to_ema_pct<=0.162026",
                    "distance_to_ema_pct": distance,
                    "distance_threshold": self._distance_threshold,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "risk_reward_ratio": settings.risk.risk_reward_ratio,
                    # Only operational change in V1.1
                    "max_holding_minutes": self._time_stop_minutes,
                    "exit_upgrade": "time_stop",
                },
            )

        return StrategySignal(
            signal=SignalType.HOLD,
            price=price,
            timestamp=timestamp,
            score=0.0,
            metadata={
                "rule": "distance_to_ema_pct<=0.162026",
                "distance_to_ema_pct": distance,
                "distance_threshold": self._distance_threshold,
            },
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = self._timestamp_from_index(last.name)
        return StrategySignal(
            signal=SignalType.HOLD,
            price=price,
            timestamp=timestamp,
            score=0.0,
            metadata={"entry_price": float(entry_price), "reason": "managed_by_engine_risk_and_time_stop"},
        )

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        distance = float(pd.to_numeric(last.get("distance_to_ema_pct"), errors="coerce") or 0.0)
        return 1.0 if distance <= self._distance_threshold else 0.0

    def event_entry_mask(self, frame: pd.DataFrame) -> pd.Series:
        distances = pd.to_numeric(frame.get("distance_to_ema_pct"), errors="coerce").fillna(0.0)
        return distances <= self._distance_threshold

    @staticmethod
    def _timestamp_from_index(value: object) -> datetime:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
        if pd.isna(ts):
            return datetime.utcnow()
        return ts.to_pydatetime()

    def _assert_initialized(self) -> None:
        if self._ema_slow is None:
            raise RuntimeError("TradeOutcomeNextGenV11Strategy is not initialized. Call initialize() first.")
