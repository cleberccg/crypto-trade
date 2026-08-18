"""
TrendV1 - first production strategy.

Logic overview
--------------
Entry (BUY) conditions (all must be true):
1. EMA_20 > EMA_50 (bullish trend alignment)
2. Price above EMA_20
3. RSI_14 between 45 and 65 (momentum without extreme overbought)
4. MACD histogram is positive and increasing
5. Price above the Bollinger middle band

Exit (SELL) conditions (any triggers exit):
1. EMA_20 crosses below EMA_50
2. RSI_14 > 70 (overbought - take profit)
3. MACD histogram turns negative
4. Price drops below lower Bollinger Band

Stop-loss / Take-profit
-----------------------
- Stop-loss: entry - (ATR × ATR_STOP_MULTIPLIER)
- Risk: entry - stop_loss
- Reward: risk × RISK_REWARD_RATIO
- Take-profit: entry + reward
- Trailing stop: default_trailing_stop_pct below the running high

This ensures that the realized RR = configured RISK_REWARD_RATIO.
"""
from __future__ import annotations

from datetime import timezone

import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.macd import MACD
from indicators.rsi import RSI
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.families import TrendStrategy
from strategies.registry import register_strategy
from utils.helpers import utc_now
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="TrendV1",
    version="v1",
    family="trend",
    description="Trend-following strategy using EMA, RSI, MACD and Bollinger filters.",
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
    indicators=["EMA", "RSI", "MACD", "BollingerBands", "ATR"],
    categories=["trend", "momentum", "spot"],
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
    aliases=["trend_v1", "v1"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class TrendV1Strategy(TrendStrategy):
    """
    Trend-following strategy using EMA, RSI, MACD, and Bollinger Bands.

    Stop-loss and take-profit are calculated to honor the configured risk/reward ratio.
    This ensures compatibility with RiskManager validation.

    Args:
        ema_fast: Fast EMA period (default 20).
        ema_slow: Slow EMA period (default 50).
        rsi_period: RSI period (default 14).
        atr_period: ATR period for stop-loss calculation (default 14).
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        rsi_min: float = 45.0,
        rsi_max: float = 65.0,
        atr_stop_multiplier: float = 2.0,
        risk_reward_ratio: float = 2.0,
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

        # Initialised in initialize()
        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None
        self._rsi: RSI | None = None
        self._macd: MACD | None = None
        self._bb: BollingerBands | None = None
        self._atr: ATR | None = None

    @property
    def name(self) -> str:
        return "TrendV1"

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Instantiate all indicator objects."""
        self._ema_fast = EMA(period=self._ema_fast_period)
        self._ema_slow = EMA(period=self._ema_slow_period)
        self._rsi = RSI(period=self._rsi_period)
        self._macd = MACD()
        self._bb = BollingerBands()
        self._atr = ATR(period=self._atr_period)
        logger.info("%s - initialized.", self.name)

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add all indicator columns to a copy of *df*.

        Columns added: ema_fast, ema_slow, rsi, macd, signal, histogram,
        bb_middle, bb_upper, bb_lower, bb_bandwidth, bb_percent_b, atr.
        """
        self._assert_initialized()
        result = df.copy()

        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]

        macd_df = self._macd.calculate(df)  # type: ignore[union-attr]
        result["macd"] = macd_df["macd"]
        result["macd_signal"] = macd_df["signal"]
        result["macd_histogram"] = macd_df["histogram"]

        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]
        result["bb_middle"] = bb_df["middle"]
        result["bb_upper"] = bb_df["upper"]
        result["bb_lower"] = bb_df["lower"]
        result["bb_percent_b"] = bb_df["percent_b"]

        return result

    # ------------------------------------------------------------------
    # Geracao de sinais
    # ------------------------------------------------------------------

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        """Generate a BUY signal when all trend conditions align."""
        self._assert_initialized()

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        price = float(last["close"])
        atr = float(last["atr"])
        volume = float(last["volume"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]

        # --- Condicoes de entrada ---
        trend_aligned = float(last[ema_fast_col]) > float(last[ema_slow_col])
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
            # Calculate stop-loss using ATR multiplier from configuration
            stop_loss = price - self._atr_stop_multiplier * atr
            
            # Calculate risk and then reward based on configured RR ratio
            risk = price - stop_loss
            rr_ratio = self._risk_reward_ratio
            reward = risk * rr_ratio
            take_profit = price + reward
            
            trailing_stop_pct = settings.risk.default_trailing_stop_pct
            confidence = self.score(df)

            if confidence * 100 < self._score_min:
                return StrategySignal(
                    signal=SignalType.HOLD,
                    price=price,
                    timestamp=timestamp,
                    score=confidence,
                )

            logger.info(
                "%s – BUY signal price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f",
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
        """Generate a SELL signal when any exit condition is triggered."""
        self._assert_initialized()

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]

        # --- Condicoes de saida ---
        ema_bearish_cross = (
            float(last[ema_fast_col]) < float(last[ema_slow_col])
            and float(prev[ema_fast_col]) >= float(prev[ema_slow_col])
        )
        rsi_overbought = float(last["rsi"]) > 70.0
        macd_turned_negative = (
            float(last["macd_histogram"]) < 0
            and float(prev["macd_histogram"]) >= 0
        )
        price_below_bb_lower = price < float(last["bb_lower"])

        exit_reason = None
        if ema_bearish_cross:
            exit_reason = "ema_bearish_cross"
        elif rsi_overbought:
            exit_reason = "rsi_overbought"
        elif macd_turned_negative:
            exit_reason = "macd_turned_negative"
        elif price_below_bb_lower:
            exit_reason = "price_below_bb_lower"

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

    # ------------------------------------------------------------------
    # Pontuacao
    # ------------------------------------------------------------------

    def score(self, df: pd.DataFrame) -> float:
        """
        Compute a composite confidence score from 0 to 1.

        Factors:
        - EMA alignment (0.25)
        - RSI in optimal zone 50â€“60 (0.25)
        - MACD histogram strength (0.25)
        - Bollinger %B position (0.25)
        """
        self._assert_initialized()
        last = df.iloc[-1]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]

        # 1. Alinhamento de EMA - binario
        ema_score = 1.0 if float(last[ema_fast_col]) > float(last[ema_slow_col]) else 0.0

        # 2. Proximidade do RSI da faixa ideal (55)
        rsi = float(last["rsi"])
        rsi_score = max(0.0, 1.0 - abs(rsi - 55.0) / 25.0)

        # 3. Histograma do MACD - positivo e forte
        hist = float(last["macd_histogram"])
        hist_score = min(1.0, max(0.0, hist / (abs(hist) + 1e-8))) if hist > 0 else 0.0

        # 4. Bollinger %B na faixa [0.4, 0.7] (zona medio-superior)
        pct_b = float(last["bb_percent_b"])
        bb_score = max(0.0, 1.0 - abs(pct_b - 0.55) / 0.45)

        total = (ema_score * 0.25 + rsi_score * 0.25 + hist_score * 0.25 + bb_score * 0.25)
        return round(float(total), 4)

    # ------------------------------------------------------------------
    # Auxiliares privados
    # ------------------------------------------------------------------

    def _assert_initialized(self) -> None:
        """Raise RuntimeError if initialize() was not called."""
        if self._ema_fast is None:
            raise RuntimeError(
                f"{self.name}: call initialize() before using the strategy."
            )
