"""
ReversaoNextGenV2 — data-driven reconstruction of H27.

This strategy keeps the execution contract of the current engine (LONG/BUY
entries) while using the validated Phase 5.6 rule model trained from the H27
cluster reconstruction.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import load

from strategies.base_strategy import SignalType, StrategySignal
from strategies.registry import register_strategy
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
from utils.logger import get_logger


logger = get_logger(__name__)

_MODEL_PATH = Path(__file__).resolve().parents[1] / "optimization" / "results" / "fase56_h27_rule_model.joblib"


@register_strategy(
    name="ReversaoNextGenV2",
    version="v2",
    family="reversal_edge",
    description="Data-driven H27 reconstruction using the validated Phase 5.6 rule model.",
    parameters=[
        "ema_fast",
        "ema_slow",
        "rsi_period",
        "atr_period",
        "atr_stop_multiplier",
        "risk_reward_ratio",
        "score_min",
        "volume_multiplier_min",
        "atr_high_threshold",
        "volume_low_threshold",
        "model_path",
    ],
    indicators=["EMA", "RSI", "BollingerBands", "ATR"],
    categories=["reversal", "long", "mean_reversion", "h27"],
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
    aliases=["reversao_v2", "reversao_next_gen_v2", "h27_v2"],
)
class ReversaoNextGenV2Strategy(ReversaoNextGenV1Strategy):
    def __init__(
        self,
        ema_fast: int = 25,
        ema_slow: int = 45,
        rsi_period: int = 14,
        atr_period: int = 14,
        atr_stop_multiplier: float = 2.5,
        risk_reward_ratio: float = 3.8,
        score_min: float = 0.6,
        volume_multiplier_min: float = 0.8,
        atr_high_threshold: float = 1.0,
        volume_low_threshold: float = 0.8,
        model_path: str | Path = _MODEL_PATH,
    ) -> None:
        super().__init__(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=rsi_period,
            atr_period=atr_period,
            atr_stop_multiplier=atr_stop_multiplier,
            risk_reward_ratio=risk_reward_ratio,
            score_min=score_min,
            volume_multiplier_min=volume_multiplier_min,
            atr_high_threshold=atr_high_threshold,
            volume_low_threshold=volume_low_threshold,
        )
        self._model_path = Path(model_path)
        self._rule_model = None
        self._pred_col = "h27_rule_prediction"
        self._prob_col = "h27_rule_probability"
        self._prediction_cache: pd.DataFrame | None = None

    @property
    def name(self) -> str:
        return "ReversaoNextGenV2"

    def initialize(self) -> None:
        super().initialize()
        if not self._model_path.exists():
            raise FileNotFoundError(f"H27 rule model not found: {self._model_path}")
        self._rule_model = load(self._model_path)
        logger.info("%s — loaded H27 rule model from %s", self.name, self._model_path)

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()
        self._assert_model_loaded()

        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        prediction = int(last.get(self._pred_col, 0))
        probability = float(last.get(self._prob_col, 0.0))

        signal = SignalType.BUY if prediction == 1 else SignalType.HOLD
        if signal == SignalType.BUY:
            stop_loss = price - (self._atr_stop_multiplier * atr)
            risk = price - stop_loss
            reward = risk * self._risk_reward_ratio
            take_profit = price + reward
        else:
            stop_loss = None
            take_profit = None

        metadata = {
            "rule_prediction": prediction,
            "rule_probability": probability,
            "atr": atr,
            "reason": "h27_rule_model" if signal == SignalType.BUY else "no_signal",
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=probability,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        self._assert_initialized()
        self._assert_model_loaded()

        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        prediction = int(last.get(self._pred_col, 0))
        probability = float(last.get(self._prob_col, 0.0))

        signal = SignalType.SELL if prediction == 0 else SignalType.HOLD
        metadata = {
            "reason": "rule_invalidated" if signal == SignalType.SELL else "rule_held",
            "rule_prediction": prediction,
            "rule_probability": probability,
            "trend_score": float(last.get("trend_score", 0.0)),
            "price": price,
            "entry_price": entry_price,
            "pnl": price - entry_price,
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=probability if signal == SignalType.SELL else 0.0,
            metadata=metadata,
        )

    def score(self, df: pd.DataFrame) -> float:
        self._assert_initialized()
        self._assert_model_loaded()
        signal = self.entry_signal(df)
        return signal.score

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        self._assert_model_loaded()

        cached_predictions = self._prediction_cache
        cached_len = len(cached_predictions) if cached_predictions is not None else 0
        result = super().calculate(df)
        if result.empty:
            return result

        same_prefix = (
            cached_predictions is not None
            and cached_len > 0
            and len(result) <= cached_len
            and result.index[-1] == cached_predictions.index[len(result) - 1]
        )
        if same_prefix:
            result[self._pred_col] = cached_predictions[self._pred_col].iloc[: len(result)].to_numpy()
            result[self._prob_col] = cached_predictions[self._prob_col].iloc[: len(result)].to_numpy()
            return result

        pred_values = np.empty(len(result), dtype=int)
        prob_values = np.empty(len(result), dtype=float)

        tail_start = 0
        if cached_predictions is not None and cached_len < len(result):
            tail_start = cached_len
            pred_values[:tail_start] = cached_predictions[self._pred_col].to_numpy(dtype=int)
            prob_values[:tail_start] = cached_predictions[self._prob_col].to_numpy(dtype=float)

        feature_frame = self._build_feature_frame_bulk(result.iloc[tail_start:])
        predictions = self._rule_model.predict(feature_frame)

        probabilities = None
        if hasattr(self._rule_model, "predict_proba"):
            probabilities = self._rule_model.predict_proba(feature_frame)

        pred_values[tail_start:] = np.asarray(predictions, dtype=int)
        if probabilities is not None and probabilities.shape[1] > 1:
            prob_values[tail_start:] = np.asarray(probabilities[:, 1], dtype=float)
        else:
            prob_values[tail_start:] = pred_values[tail_start:].astype(float)

        result[self._pred_col] = pred_values
        result[self._prob_col] = prob_values

        self._prediction_cache = result[[self._pred_col, self._prob_col]].copy()

        return result

    def _assert_model_loaded(self) -> None:
        if self._rule_model is None:
            raise RuntimeError(f"{self.name} not initialized with the H27 rule model. Call initialize() first.")

    def _build_feature_frame(self, last: pd.Series, timestamp: datetime) -> pd.DataFrame:
        ema_slow_col = f"ema_{self._ema_slow_period}"
        distance_to_ema_pct = (float(last["close"]) - float(last[ema_slow_col])) / float(last[ema_slow_col]) * 100.0
        hour = int(timestamp.hour)
        weekday = timestamp.strftime("%A")
        day_type = "weekday" if timestamp.weekday() < 5 else "weekend"
        entry_session = self._session_from_hour(hour)

        return pd.DataFrame(
            [
                {
                    "trend_score": float(last.get("trend_score", 0.0)),
                    "trend_score_prev": float(last.get("trend_score_prev", 0.0)),
                    "atr": float(last.get("atr", 0.0)),
                    "rsi": float(last.get("rsi", 50.0)),
                    "bb_percent_b": float(last.get("bb_percent_b", 0.5)),
                    "relative_volume": float(last.get("relative_volume", 1.0)),
                    "distance_to_ema_pct": float(distance_to_ema_pct),
                    "hour": hour,
                    "atr_bucket": str(last.get("atr_bucket", "unknown")),
                    "volume_bucket": str(last.get("volume_bucket", "unknown")),
                    "bollinger_position": str(last.get("bollinger_position", "unknown")),
                    "weekday": weekday,
                    "day_type": day_type,
                    "entry_session": entry_session,
                }
            ]
        )

    def _build_feature_frame_bulk(self, df: pd.DataFrame) -> pd.DataFrame:
        ema_slow_col = f"ema_{self._ema_slow_period}"
        ts = pd.DatetimeIndex(df.index)
        hour = ts.hour
        weekday = ts.strftime("%A")
        day_type = np.where(ts.dayofweek < 5, "weekday", "weekend")

        entry_session = np.select(
            [
                (hour >= 0) & (hour <= 4),
                (hour >= 5) & (hour <= 10),
                (hour >= 11) & (hour <= 16),
                (hour >= 17) & (hour <= 23),
            ],
            ["open_asia", "europe", "us_open", "us_late"],
            default="overnight",
        )

        distance_to_ema_pct = (df["close"].astype(float) - df[ema_slow_col].astype(float)) / df[ema_slow_col].astype(float) * 100.0

        return pd.DataFrame(
            {
                "trend_score": df["trend_score"].astype(float),
                "trend_score_prev": df["trend_score_prev"].astype(float),
                "atr": df["atr"].astype(float),
                "rsi": df["rsi"].astype(float),
                "bb_percent_b": df["bb_percent_b"].astype(float),
                "relative_volume": df["relative_volume"].astype(float),
                "distance_to_ema_pct": distance_to_ema_pct.astype(float),
                "hour": hour.astype(int),
                "atr_bucket": df["atr_bucket"].astype(str),
                "volume_bucket": df["volume_bucket"].astype(str),
                "bollinger_position": df["bollinger_position"].astype(str),
                "weekday": weekday,
                "day_type": day_type,
                "entry_session": entry_session,
            },
            index=df.index,
        )

    @staticmethod
    def _session_from_hour(hour: int) -> str:
        if 0 <= hour <= 4:
            return "open_asia"
        if 5 <= hour <= 10:
            return "europe"
        if 11 <= hour <= 16:
            return "us_open"
        if 17 <= hour <= 23:
            return "us_late"
        return "overnight"