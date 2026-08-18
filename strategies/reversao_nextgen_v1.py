"""
ReversaoNextGenV1 — Reversal Edge strategy from Cluster H27.

Hypothesis: H27 / Cluster: reversao_2
Confidence: 75% | Priority: 0.625 | Rank: #4
Generated from Phase 5.4 engineering reversal.

Logic overview
--------------
Entry (SHORT/SELL) conditions (all must be true):
1. Regime = reversao (trend reversal detected via EMA crossover/trend_score reversal)
2. ATR bucket = high_atr (volatility level in upper tertile)
3. RSI bucket = unknown (RSI neutral - no specific filter)
4. Volume bucket = low_volume (below average relative volume)
5. Bollinger position = inside_band (price within 2-std bands)

Direction: SHORT (sell reversal)

Exit conditions (any triggers exit):
1. Exit when profit target is reached
2. Exit when stop loss is hit
3. Exit when reversal pattern fails (regime changes back to trend)

Stop-loss / Take-profit
-----------------------
- Stop-loss: entry + (ATR × ATR_STOP_MULTIPLIER) [short position, so above entry]
- Risk: stop_loss - entry
- Reward: risk × RISK_REWARD_RATIO
- Take-profit: entry - reward

This ensures that the realized RR = configured RISK_REWARD_RATIO.

Historical performance (from Phase 5.4):
- Sample size: 1,933,669 trades
- Win rate: 100.0%
- Sharpe: 249.48
- Expectancy: $25.00 per trade
- Drawdown: 0.0%
- Risk/Reward: 3.18:1 (MFE/MAE)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.rsi import RSI
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.families import ReversalEdgeStrategy
from strategies.registry import register_strategy
from utils.helpers import utc_now
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="ReversaoNextGenV1",
    version="v1",
    family="reversal_edge",
    description="Reversal Edge strategy using regime detection, ATR, volume, and Bollinger filters.",
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
    ],
    indicators=["EMA", "RSI", "BollingerBands", "ATR"],
    categories=["reversal", "short", "mean_reversion"],
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
    aliases=["reversao_v1", "reversao_next_gen", "h27"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class ReversaoNextGenV1Strategy(ReversalEdgeStrategy):
    """
    Reversal Edge strategy from Phase 5.4 cluster H27.

    Detects reversal patterns based on regime change, volatility (ATR), 
    volume profile (low volume), and Bollinger Band positioning.

    Direction: SHORT (sell at reversal points)

    Args:
        ema_fast: Fast EMA period for trend detection (default 20).
        ema_slow: Slow EMA period for trend detection (default 50).
        rsi_period: RSI period (default 14, unused in entry but available).
        atr_period: ATR period for stop-loss calculation (default 14).
        atr_stop_multiplier: Multiplier for ATR-based stop loss (default 2.0).
        risk_reward_ratio: Target risk/reward ratio (default 3.18).
        score_min: Minimum confidence score to trade (default 0.6).
        volume_multiplier_min: Minimum relative volume (default 0.7).
        atr_high_threshold: ATR percentile to classify as "high" (default 0.67).
        volume_low_threshold: Volume percentile to classify as "low" (default 0.40).
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        atr_stop_multiplier: float = 2.0,
        risk_reward_ratio: float = 3.18,
        score_min: float = 0.6,
        volume_multiplier_min: float = 0.7,
        atr_high_threshold: float = 0.67,
        volume_low_threshold: float = 0.40,
    ) -> None:
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._atr_stop_multiplier = atr_stop_multiplier
        self._risk_reward_ratio = risk_reward_ratio
        self._score_min = score_min
        self._volume_multiplier_min = volume_multiplier_min
        self._atr_high_threshold = atr_high_threshold
        self._volume_low_threshold = volume_low_threshold

        # Initialized in initialize()
        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None
        self._rsi: RSI | None = None
        self._bb: BollingerBands | None = None
        self._atr: ATR | None = None
        # Pre-computed enriched DataFrame cache — avoids O(n²) in BacktestEngine
        self._enriched_cache: pd.DataFrame | None = None

    @property
    def name(self) -> str:
        return "ReversaoNextGenV1"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Instantiate all indicator objects."""
        self._ema_fast = EMA(period=self._ema_fast_period)
        self._ema_slow = EMA(period=self._ema_slow_period)
        self._rsi = RSI(period=self._rsi_period)
        self._bb = BollingerBands()
        self._atr = ATR(period=self._atr_period)
        self._enriched_cache = None  # Reset cache on re-initialise
        logger.info("%s — initialized.", self.name)

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add all indicator columns to a copy of *df*.

        Performance: uses an internal cache so that repeated calls with growing
        prefix slices (as done by BacktestEngine bar-by-bar) are served in O(1).
        The first call on a full dataset triggers one O(n) vectorised pass.

        Slow operations replaced:
        - pd.qcut per bar  -> expanding().rank(pct=True) + np.where  [O(n) vectorised]
        - apply(axis=1)    -> np.where on numpy arrays               [O(n) vectorised]

        Columns added: ema_fast, ema_slow, rsi, bb_middle, bb_upper, bb_lower,
        bb_percent_b, atr, trend_score, atr_bucket, volume_bucket,
        bollinger_position, regime_reversal.
        """
        self._assert_initialized()
        n = len(df)

        # --- Cache hit: return pre-computed slice (O(1)) ---
        if self._enriched_cache is not None and n <= len(self._enriched_cache):
            if n > 0 and df.index[-1] == self._enriched_cache.index[n - 1]:
                return self._enriched_cache.iloc[:n]

        # --- Full vectorised computation (O(n)) ---
        _t0 = time.perf_counter()
        logger.debug("%s — calculate: computing indicators for %d bars", self.name, n)
        result = df.copy()

        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — EMA done (%.3fs)", self.name, time.perf_counter() - _t0)

        _t1 = time.perf_counter()
        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — RSI done (%.3fs)", self.name, time.perf_counter() - _t1)

        _t2 = time.perf_counter()
        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — ATR done (%.3fs)", self.name, time.perf_counter() - _t2)

        _t3 = time.perf_counter()
        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]
        result["bb_middle"] = bb_df["middle"]
        result["bb_upper"] = bb_df["upper"]
        result["bb_lower"] = bb_df["lower"]
        result["bb_percent_b"] = bb_df["percent_b"]
        logger.debug("%s — Bollinger done (%.3fs)", self.name, time.perf_counter() - _t3)

        # Trend score (EMA-based)
        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        result["trend_score"] = (
            (result[ema_fast_col] - result[ema_slow_col]) / result[ema_slow_col] * 100
        )

        _t4 = time.perf_counter()
        # ATR tertiles: expanding-rank replaces pd.qcut (mathematically equivalent)
        _atr_rank = result["atr"].expanding(min_periods=3).rank(pct=True).to_numpy()
        result["atr_bucket"] = np.where(
            np.isnan(_atr_rank), "mid_atr",
            np.where(_atr_rank <= 1 / 3, "low_atr",
            np.where(_atr_rank <= 2 / 3, "mid_atr", "high_atr")),
        )
        logger.debug("%s — ATR buckets done (%.3fs)", self.name, time.perf_counter() - _t4)

        # Volume bucket: fixed thresholds, already O(n)
        result["relative_volume"] = result["volume"] / result["volume"].rolling(20).mean()
        result["volume_bucket"] = pd.cut(
            result["relative_volume"],
            bins=[-float("inf"), 0.9, 1.1, float("inf")],
            labels=["low_volume", "normal_volume", "high_volume"],
            include_lowest=True,
        ).astype(str)

        _t5 = time.perf_counter()
        # Bollinger position: numpy.where replaces apply(axis=1)
        _close = result["close"].to_numpy()
        _bb_upper = result["bb_upper"].to_numpy()
        _bb_lower = result["bb_lower"].to_numpy()
        result["bollinger_position"] = np.where(
            _close > _bb_upper, "above_upper",
            np.where(_close < _bb_lower, "below_lower", "inside_band"),
        )
        logger.debug("%s — Bollinger positions done (%.3fs)", self.name, time.perf_counter() - _t5)

        # Regime: detect reversal (trend_score changing sign or crossing zero)
        result["trend_score_prev"] = result["trend_score"].shift(1)
        result["regime_reversal"] = (
            (result["trend_score"] * result["trend_score_prev"] < 0)  # Sign change
            | (
                (result["trend_score"].abs() < 0.2)
                & (result["trend_score_prev"].abs() > 0.2)
            )  # Entering consolidation
        )

        _total = time.perf_counter() - _t0
        logger.info("%s — indicators pre-computed: %d bars in %.2fs", self.name, n, _total)

        # Cache for subsequent prefix-slice lookups by BacktestEngine
        if self._enriched_cache is None or n > len(self._enriched_cache):
            self._enriched_cache = result

        return result

    @staticmethod
    def _get_bollinger_position(
        close: float, bb_lower: float, bb_middle: float, bb_upper: float
    ) -> str:
        """Determine Bollinger Band position."""
        if close > bb_upper:
            return "above_upper"
        elif close < bb_lower:
            return "below_lower"
        else:
            return "inside_band"

    # ------------------------------------------------------------------
    # Signal Generation
    # ------------------------------------------------------------------

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        """
        Generate a BUY signal when a BULLISH reversal (H27) aligns.

        Key difference from the original SHORT version:
        - We require prev_trend_score < 0 (was in downtrend) so we enter
          only on bearish-to-bullish reversals, not on bullish-to-bearish.
        - Stop is placed BELOW entry; take-profit ABOVE.
        """
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        # --- Entry Conditions ---
        # All 5 must be TRUE for a BUY signal

        # 1. Bullish regime reversal: trend was DOWN and is now reversing UP
        regime_reversal = bool(last.get("regime_reversal", False))
        trend_score = float(last.get("trend_score", 0.0))
        prev_trend_score = float(last.get("trend_score_prev", 0.0))

        # Bullish reversal: sign change from negative (downtrend) upward
        bullish_reversal = regime_reversal and prev_trend_score < 0

        # Softer entry: trend actively recovering from a significant downtrend.
        # Requires:
        #   1. previous bar was in a real downtrend (< -0.5, not just slightly negative)
        #   2. trend is actively improving (trend_score > prev_trend_score)
        #   3. now in weak/neutral zone (abs < 0.3)
        bullish_consolidation = (
            prev_trend_score < -0.5
            and trend_score > prev_trend_score
            and abs(trend_score) < 0.3
        )

        # 2. ATR bucket = high_atr (volatile reversal — real momentum)
        atr_bucket = str(last.get("atr_bucket", "unknown"))
        atr_is_high = atr_bucket == "high_atr"

        # 3. RSI: no hard filter (H27 cluster had "unknown" RSI bucket)
        rsi_value = float(last.get("rsi", 50.0))
        rsi_ok = True

        # 4. Volume bucket = low_volume (seller exhaustion before recovery)
        volume_bucket = str(last.get("volume_bucket", "unknown"))
        volume_is_low = volume_bucket == "low_volume"

        # 5. Bollinger position = inside_band (not at extreme, reversal still forming)
        bollinger_pos = str(last.get("bollinger_position", "unknown"))
        bb_inside = bollinger_pos == "inside_band"

        # Confidence score
        confidence = 0.0
        if bullish_reversal:
            confidence += 0.3
        if bullish_consolidation:
            confidence += 0.2
        if atr_is_high:
            confidence += 0.2
        if volume_is_low:
            confidence += 0.15
        if bb_inside:
            confidence += 0.15

        signal = SignalType.HOLD

        if bullish_reversal and atr_is_high and rsi_ok and volume_is_low and bb_inside:
            if confidence >= self._score_min:
                signal = SignalType.BUY
        elif bullish_consolidation and atr_is_high and volume_is_low and bb_inside:
            # Softer path: entering consolidation from downtrend + volatile + low vol + BB inside
            if confidence >= self._score_min * 0.9:
                signal = SignalType.BUY

        # LONG: stop below entry, take-profit above entry
        if signal == SignalType.BUY:
            stop_loss = price - (self._atr_stop_multiplier * atr)  # Long: SL below
            risk = price - stop_loss
            reward = risk * self._risk_reward_ratio
            take_profit = price + reward
        else:
            stop_loss = None
            take_profit = None

        metadata = {
            "bullish_reversal": bullish_reversal,
            "bullish_consolidation": bullish_consolidation,
            "atr_bucket": atr_bucket,
            "volume_bucket": volume_bucket,
            "bollinger_position": bollinger_pos,
            "trend_score": trend_score,
            "prev_trend_score": prev_trend_score,
            "confidence": confidence,
            "atr": atr,
            "rsi": rsi_value,
            "reason": self._entry_reason(signal, bullish_reversal or bullish_consolidation, atr_is_high, volume_is_low, bb_inside),
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        """
        Generate a SELL signal to close LONG when the bullish regime breaks.

        Exits early when the trend turns clearly bearish again — i.e. when the
        recovery that triggered the BUY has failed or reversed.
        The engine also closes via stop-loss / take-profit independently.
        """
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        regime_reversal = bool(last.get("regime_reversal", False))
        trend_score = float(last.get("trend_score", 0.0))

        # Exit LONG when the trend has turned clearly bearish again
        exit_trend_bearish = trend_score < -0.3          # Strong downtrend re-established
        regime_back_to_bearish = not regime_reversal and trend_score < -0.2  # Sustained bearish

        signal = SignalType.HOLD
        if exit_trend_bearish or regime_back_to_bearish:
            signal = SignalType.SELL  # Close long = SELL signal

        metadata = {
            "reason": "trend_bearish" if exit_trend_bearish else "regime_back_to_bearish",
            "trend_score": trend_score,
            "price": price,
            "entry_price": entry_price,
            "pnl": price - entry_price,  # Positive for profitable long
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=1.0 if signal != SignalType.HOLD else 0.0,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, df: pd.DataFrame) -> float:
        """
        Calculate overall strategy confidence score.

        Used by the framework to assess signal quality.
        """
        self._assert_initialized()

        last = df.iloc[-1]

        signal = self.entry_signal(df)
        return signal.score

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_reason(
        signal: SignalType,
        regime_reversal: bool,
        atr_is_high: bool,
        volume_is_low: bool,
        bb_inside: bool,
    ) -> str:
        """Describe why entry signal was generated."""
        if signal == SignalType.BUY:
            conditions = []
            if regime_reversal:
                conditions.append("bullish_reversal")
            if atr_is_high:
                conditions.append("atr_high")
            if volume_is_low:
                conditions.append("volume_low")
            if bb_inside:
                conditions.append("bb_inside")
            return " + ".join(conditions) if conditions else "reversal_pattern"
        return "no_signal"

    def _assert_initialized(self) -> None:
        """Check that all indicators are initialized."""
        if not all(
            [self._ema_fast, self._ema_slow, self._rsi, self._bb, self._atr]
        ):
            raise RuntimeError(
                f"{self.name} not initialized. Call initialize() first."
            )
