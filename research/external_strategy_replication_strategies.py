"""External strategy replication: reproduce published trend-following specs
(BTC 4H SMA200, Apex No-Pyramid, Quattro Donchian, 5 EMA Weekly Filter,
Multi-Asset Vol-Normalized Trend) and gate them through DEV -> Validation ->
OOS on our own Binance Spot dataset.

Reuses, unmodified:
- BacktestEngine / BacktestConfig (backtesting/engine.py)
- RiskManager defaults (risk/risk_manager.py) -- not touched, not subclassed
- compute_metrics (backtesting/metrics.py)
- CandleRepository (database/repositories.py) over the existing MySQL candles
- FINAL_HOLDOUT convention already used elsewhere in this repo: rows with
  timestamp >= 2026-06-01 are NEVER loaded here (structurally, not just
  filtered after the fact).

Does not touch Paper Live, CDB (ClassicDonchianBreakout), RiskManager or
PositionSizer. New strategy classes are plain BaseStrategy subclasses defined
below (not registered in strategies/registry.py), so they are invisible to
the optimizer/paper-live strategy catalog.

Base OHLCV granularity: 15m (longest common history per symbol in the DB),
resampled with standard OHLCV aggregation to 4h/1D/1W-MON as each strategy
requires. This is NOT new data collection -- it is a deterministic
aggregation of candles already validated and stored by this project.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtesting.metrics import compute_metrics
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.mean_reversion_v1 import MeanReversionV1Strategy

BASE_DIR = Path(__file__).resolve().parent
OUT_JSON = BASE_DIR / "external_strategy_replication_latest.json"

FINAL_HOLDOUT_START = pd.Timestamp("2026-06-01T00:00:00Z")
BASE_FEE = 0.001      # 0.1% per side, project baseline (backtesting/engine.py _DEFAULT_FEE_PCT)
STRESS_FEE = 0.0015   # 0.15% per side, same convention as run_autonomous_strategy_research_v3.py
SLIPPAGE_BPS = 2.0    # extra one-way stress cost, same convention as run_autonomous_strategy_research_v3.py
CAPITAL = 10_000.0
BASE_TIMEFRAME = "15m"

# Neutralize the engine's built-in stop/TP/percentage-trailing when a strategy's
# published spec has no such mechanism (or implements its own via exit_signal).
# Values are chosen so they are, for all practical purposes, never reached.
NEVER_STOP_FRACTION = 0.01     # stop_loss = entry * 0.01  (99% adverse move)
NEVER_TP_MULTIPLE = 100.0      # take_profit = entry * 100
NEVER_TRAILING_PCT = 0.99      # 99% pullback from peak

TARGET_CANDIDATES = 1
CANDIDATE_MIN_NET_PF = 1.20
CANDIDATE_MIN_TRADES_PER_SPLIT = 10


def _log(message: str) -> None:
    print(message, flush=True)


def _ts(value: object) -> datetime:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return datetime.now(tz=timezone.utc)
    return ts.to_pydatetime()


# ---------------------------------------------------------------------------
# Data: load 15m candles from the existing DB, resample, split, holdout-lock
# ---------------------------------------------------------------------------

def load_base_candles(symbol: str) -> pd.DataFrame:
    """15m candles up to (but excluding) FINAL_HOLDOUT_START. Never loads the holdout."""
    with get_session() as session:
        repo = CandleRepository(session)
        start = datetime(2015, 1, 1, tzinfo=timezone.utc)
        end = FINAL_HOLDOUT_START.to_pydatetime() - pd.Timedelta(minutes=15)
        rows = repo.get_range(symbol, BASE_TIMEFRAME, start, end)
    if not rows:
        raise RuntimeError(f"No {BASE_TIMEFRAME} candles for {symbol}")
    df = pd.DataFrame(
        [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in rows],
        index=pd.DatetimeIndex([c.open_time for c in rows], tz="UTC"),
    )
    df = df.sort_index()
    df = df[df.index < FINAL_HOLDOUT_START]
    return df


_RESAMPLE_RULE = {"4h": "4h", "1d": "1D", "1w": "1W-MON"}


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = _RESAMPLE_RULE[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


@dataclass(frozen=True)
class Split:
    name: str
    frame: pd.DataFrame


def dev_val_oos_split(df: pd.DataFrame) -> list[Split]:
    """60/20/20 by bar count, temporal (no shuffling), FINAL_HOLDOUT already excluded upstream."""
    n = len(df)
    dev_end = int(n * 0.60)
    val_end = int(n * 0.80)
    return [
        Split("DEV", df.iloc[:dev_end]),
        Split("VALIDATION", df.iloc[dev_end:val_end]),
        Split("OOS", df.iloc[val_end:]),
    ]


# ---------------------------------------------------------------------------
# Strategy 1 -- BTC 4H SMA200 TREND (iolufemi/crypto-trend-research)
# ---------------------------------------------------------------------------

class Sma200TrendStrategy(BaseStrategy):
    """Long/flat: long while close > SMA200, flat while close < SMA200.
    No fixed take-profit, no trailing (published spec has none); engine's
    stop/TP/trailing are neutralized so the SMA flip is the only exit."""

    def __init__(self, sma_period: int = 200) -> None:
        self._period = int(sma_period)

    @property
    def name(self) -> str:
        return f"ExtRepl_SMA{self._period}TrendBTC4H"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["close"].rolling(self._period).mean()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        sma = last.get("sma")
        if sma is not None and not pd.isna(sma) and price > float(sma):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        sma = last.get("sma")
        if sma is not None and not pd.isna(sma) and price < float(sma):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "sma_flip"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        sma = last.get("sma")
        if sma is None or pd.isna(sma):
            return 0.0
        return 1.0 if float(last["close"]) > float(sma) else 0.0


class Sma200VolTargetStrategy(Sma200TrendStrategy):
    """Simple SMA200 flip, but score (=position-size multiplier via RiskManager)
    is scaled inversely to realized volatility (20-bar stdev of returns),
    approximating the original vol-targeting overlay without touching
    RiskManager/PositionSizer -- only the strategy's own `score` output."""

    def __init__(self, sma_period: int = 200, vol_window: int = 20, target_vol: float = 0.02) -> None:
        super().__init__(sma_period)
        self._vol_window = int(vol_window)
        self._target_vol = float(target_vol)

    @property
    def name(self) -> str:
        return f"ExtRepl_SMA{self._period}VolTargetBTC4H"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().calculate(df)
        ret = out["close"].pct_change()
        out["realized_vol"] = ret.rolling(self._vol_window).std()
        return out

    def _vol_score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        vol = last.get("realized_vol")
        if vol is None or pd.isna(vol) or vol <= 0:
            return 1.0
        return float(np.clip(self._target_vol / float(vol), 0.25, 1.5))

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        signal = super().entry_signal(df)
        if signal.signal == SignalType.BUY:
            signal.score = self._vol_score(df)
        return signal

    def score(self, df: pd.DataFrame) -> float:
        base = super().score(df)
        return base * self._vol_score(df) if base > 0 else 0.0


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX, causal (ewm only looks backward). Shared by regime-gated
    strategies and diagnose_oos_failure.py -- single implementation, no
    duplication."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


class Sma200RegimeGatedStrategy(Sma200TrendStrategy):
    """Experimental regime gate on top of the UNCHANGED SMA200 rule: long is
    only permitted while regime == TRENDING_BULL (close>SMA200 AND SMA200
    rising AND ADX(14)>=adx_threshold); every other regime -> CASH (flat).
    No change to entry/exit price logic, no parameter optimization -- this is
    a gate on top of the same signal, causal (ADX via ewm, slope via diff),
    no lookahead."""

    def __init__(self, sma_period: int = 200, adx_period: int = 14, adx_threshold: float = 20.0, slope_lookback: int = 20) -> None:
        super().__init__(sma_period)
        self._adx_period = int(adx_period)
        self._adx_threshold = float(adx_threshold)
        self._slope_lookback = int(slope_lookback)

    @property
    def name(self) -> str:
        return "Sma200RegimeGated"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().calculate(df)
        out["adx"] = compute_adx(out, self._adx_period)
        out["sma_slope_up"] = out["sma"] > out["sma"].shift(self._slope_lookback)
        out["regime_bull"] = (out["close"] > out["sma"]) & out["sma_slope_up"].fillna(False) & (out["adx"] >= self._adx_threshold)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if not bool(last.get("regime_bull", False)):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "regime_not_bull"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if bool(last.get("regime_bull", False)) else 0.0


class RegimeAdaptiveStrategy(BaseStrategy):
    """TRENDING_BULL -> SMA200 (unchanged rule); SIDEWAYS (ADX<20, not bull) ->
    delegate to the existing, unmodified MeanReversionV1Strategy; anything else
    (TRENDING_BEAR / uncertain) -> CASH. Only tested if regime-gating alone
    (Sma200RegimeGatedStrategy) already showed a clear improvement (etapa 5
    gate). Reuses MeanReversionV1Strategy as-is (not registered, not modified,
    not selected among several -- exactly one mean-reversion strategy)."""

    def __init__(self, sma_period: int = 200, adx_period: int = 14, adx_threshold: float = 20.0, slope_lookback: int = 20) -> None:
        self._period = int(sma_period)
        self._adx_period = int(adx_period)
        self._adx_threshold = float(adx_threshold)
        self._slope_lookback = int(slope_lookback)
        self._mr = MeanReversionV1Strategy()

    @property
    def name(self) -> str:
        return "ExtRepl_RegimeAdaptive_SMA200_MR"

    def initialize(self) -> None:
        self._mr.initialize()

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["close"].rolling(self._period).mean()
        out["adx"] = compute_adx(out, self._adx_period)
        out["sma_slope_up"] = out["sma"] > out["sma"].shift(self._slope_lookback)
        out["regime_bull"] = (out["close"] > out["sma"]) & out["sma_slope_up"].fillna(False) & (out["adx"] >= self._adx_threshold)
        out["regime_sideways"] = (~out["regime_bull"]) & (out["adx"] < self._adx_threshold)
        mr_cols = self._mr.calculate(df)
        for col in mr_cols.columns:
            if col not in out.columns:
                out[col] = mr_cols[col]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        if bool(last.get("regime_sideways", False)):
            return self._mr.entry_signal(df)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if bool(last.get("regime_sideways", False)):
            return self._mr.exit_signal(df, entry_price)
        return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "regime_bear_or_uncertain"})

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if bool(last.get("regime_bull", False)):
            return 1.0
        if bool(last.get("regime_sideways", False)):
            return self._mr.score(df)
        return 0.0


# ---------------------------------------------------------------------------
# Strategy 2 -- APEX NO-PYRAMID (EstebanSP23/crypto_systematic_research)
# ---------------------------------------------------------------------------

class ApexNoPyramidStrategy(BaseStrategy):
    """4h breakout of ~6-month high, SMA50>SMA200 trend filter, volume>1.5x avg.
    DEVIATION FROM PUBLISHED SPEC (documented, not silently simplified): the
    platform's BacktestEngine tracks a single full position per symbol with no
    partial-close support, so the published '50% scale-out at 2R + trail rest
    by 20-bar low' cannot be reproduced exactly. We approximate with a single
    full exit on a 20-bar-low trailing stop (the trailing component of the
    original rule), which is the closest faithful behavior the engine allows.
    No pyramiding is structurally guaranteed (engine never opens a 2nd
    position while one is open), matching 'SEM pyramiding' exactly.
    """

    def __init__(self, breakout_bars: int = 1095, sma_fast: int = 50, sma_slow: int = 200, volume_window: int = 20, volume_multiple: float = 1.5, trail_bars: int = 20) -> None:
        self._breakout_bars = int(breakout_bars)
        self._sma_fast = int(sma_fast)
        self._sma_slow = int(sma_slow)
        self._volume_window = int(volume_window)
        self._volume_multiple = float(volume_multiple)
        self._trail_bars = int(trail_bars)

    @property
    def name(self) -> str:
        return "ExtRepl_ApexNoPyramid4H"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["breakout_high"] = out["high"].rolling(self._breakout_bars).max().shift(1)
        out["sma_fast"] = out["close"].rolling(self._sma_fast).mean()
        out["sma_slow"] = out["close"].rolling(self._sma_slow).mean()
        out["avg_volume"] = out["volume"].rolling(self._volume_window).mean()
        out["trail_low"] = out["low"].rolling(self._trail_bars).min().shift(1)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        cols = ["breakout_high", "sma_fast", "sma_slow", "avg_volume", "trail_low"]
        if any(pd.isna(last.get(c)) for c in cols):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        trend_ok = float(last["sma_fast"]) > float(last["sma_slow"])
        breakout_ok = price > float(last["breakout_high"])
        volume_ok = float(last["volume"]) > self._volume_multiple * float(last["avg_volume"])
        if trend_ok and breakout_ok and volume_ok:
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        trail = last.get("trail_low")
        if trail is not None and not pd.isna(trail) and price < float(trail):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "trail_20bar_low"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if any(pd.isna(last.get(c)) for c in ("sma_fast", "sma_slow")):
            return 0.0
        return 1.0 if float(last["sma_fast"]) > float(last["sma_slow"]) else 0.0


# ---------------------------------------------------------------------------
# Strategy 3 -- QUATTRO DONCHIAN (EstebanSP23/crypto_systematic_research)
# ---------------------------------------------------------------------------

class QuattroDonchianStrategy(BaseStrategy):
    """BTC 4h Donchian(20) breakout, daily EMA200 rising filter, ATR(14),
    2xATR chandelier trailing stop, 5% catastrophe stop.
    DEVIATION FROM PUBLISHED SPEC (documented): the original strategy pyramids
    up to 4 units at +0.5N intervals. The platform's engine supports exactly
    one open position per symbol (no adding to a position), so pyramiding is
    NOT reproduced -- this tests the single-unit base case only. This does
    not require leverage/perpetuals; it runs as a plain long/flat spot
    position, so no leverage distortion is introduced.
    """

    def __init__(self, donchian_window: int = 20, atr_period: int = 14, atr_multiple: float = 2.0, catastrophe_stop_pct: float = 0.05) -> None:
        self._window = int(donchian_window)
        self._atr_period = int(atr_period)
        self._atr_multiple = float(atr_multiple)
        self._catastrophe_stop_pct = float(catastrophe_stop_pct)
        self._peak_since_entry: float | None = None

    @property
    def name(self) -> str:
        return "ExtRepl_QuattroDonchianBTC4H"

    def initialize(self) -> None:
        self._peak_since_entry = None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["donchian_high"] = out["high"].rolling(self._window).max().shift(1)
        high, low, close = out["high"], out["low"], out["close"]
        prev_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(alpha=1.0 / self._atr_period, adjust=False, min_periods=self._atr_period).mean()
        # Daily EMA200 filter, computed on this symbol's own daily resample of the
        # SAME already-loaded candles (no new data), merged causally (as-of, no lookahead).
        daily = out[["open", "high", "low", "close", "volume"]].resample("1D", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        daily["ema200"] = daily["close"].ewm(span=200, adjust=False).mean()
        daily["ema200_rising"] = daily["ema200"] > daily["ema200"].shift(20)
        merged = pd.merge_asof(out.reset_index(), daily[["ema200_rising"]].reset_index().rename(columns={"index": "day"}),
                                left_on="index", right_on="day", direction="backward")
        out["ema200_rising"] = merged["ema200_rising"].to_numpy()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("donchian_high")) or pd.isna(last.get("atr")) or not bool(last.get("ema200_rising", False)):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if price > float(last["donchian_high"]):
            self._peak_since_entry = price
            catastrophe_stop = price * (1.0 - self._catastrophe_stop_pct)
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=catastrophe_stop,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        atr = last.get("atr")
        if atr is None or pd.isna(atr):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if self._peak_since_entry is None:
            self._peak_since_entry = max(price, entry_price)
        self._peak_since_entry = max(self._peak_since_entry, price)
        chandelier = self._peak_since_entry - self._atr_multiple * float(atr)
        if price < chandelier:
            self._peak_since_entry = None
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "chandelier_2atr"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if bool(last.get("ema200_rising", False)) else 0.0


# ---------------------------------------------------------------------------
# Strategy 4 -- 5 EMA TREND FILTER (weekly)
# ---------------------------------------------------------------------------

class FiveEmaWeeklyFilterStrategy(BaseStrategy):
    """BTC weekly close > EMA5 (weekly), AND daily EMA200 rising vs 20 days ago.
    Long/flat, exits when the weekly condition ceases. Low frequency by design."""

    def __init__(self, weekly_ema_period: int = 5, daily_ema_period: int = 200, daily_lookback: int = 20) -> None:
        self._weekly_ema_period = int(weekly_ema_period)
        self._daily_ema_period = int(daily_ema_period)
        self._daily_lookback = int(daily_lookback)

    @property
    def name(self) -> str:
        return "ExtRepl_5EMAWeeklyFilterBTC"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        # df here is already the WEEKLY frame (see prepare_weekly_frame below);
        # "daily_ema200_rising" is pre-merged as a column before this call.
        out = df.copy()
        out["weekly_ema5"] = out["close"].ewm(span=self._weekly_ema_period, adjust=False).mean()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        ema = last.get("weekly_ema5")
        daily_rising = bool(last.get("daily_ema200_rising", False))
        if ema is not None and not pd.isna(ema) and price > float(ema) and daily_rising:
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        ema = last.get("weekly_ema5")
        daily_rising = bool(last.get("daily_ema200_rising", False))
        if (ema is not None and not pd.isna(ema) and price < float(ema)) or not daily_rising:
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "weekly_condition_lost"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        ema = last.get("weekly_ema5")
        if ema is None or pd.isna(ema):
            return 0.0
        return 1.0 if float(last["close"]) > float(ema) and bool(last.get("daily_ema200_rising", False)) else 0.0


def prepare_weekly_frame(base_15m: pd.DataFrame, daily_ema_period: int, daily_lookback: int) -> pd.DataFrame:
    daily = resample_ohlcv(base_15m, "1d")
    daily["ema200"] = daily["close"].ewm(span=daily_ema_period, adjust=False).mean()
    daily["ema200_rising"] = daily["ema200"] > daily["ema200"].shift(daily_lookback)
    weekly = resample_ohlcv(base_15m, "1w")
    merged = pd.merge_asof(
        weekly.reset_index(), daily[["ema200_rising"]].reset_index().rename(columns={"index": "day"}),
        left_on="index", right_on="day", direction="backward",
    )
    weekly["daily_ema200_rising"] = merged["ema200_rising"].to_numpy()
    return weekly


# ---------------------------------------------------------------------------
# Strategy 5 -- MULTI-ASSET VOLATILITY-NORMALIZED TREND
# ---------------------------------------------------------------------------

class VolNormalizedTrendStrategy(BaseStrategy):
    """Daily SMA50/SMA200 trend-following, position score inversely scaled to
    20-day realized volatility (vol-normalized sizing proxy via `score`).
    NOTE: the original public spec ('medias moveis / trend following',
    'sizing de portfolio', 'walk-forward') does not give exact MA periods; we
    use the standard 50/200 golden-cross convention and document this choice
    explicitly rather than guessing undocumented parameters. Portfolio-level
    capital allocation across BTC/ETH/BNB/ADA is NOT natively supported by the
    single-symbol BacktestEngine; each asset is run independently and results
    are pooled/aggregated afterwards as an approximation of the portfolio
    mechanism -- documented, not silently presented as identical."""

    def __init__(self, sma_fast: int = 50, sma_slow: int = 200, vol_window: int = 20, target_vol: float = 0.02) -> None:
        self._fast = int(sma_fast)
        self._slow = int(sma_slow)
        self._vol_window = int(vol_window)
        self._target_vol = float(target_vol)

    @property
    def name(self) -> str:
        return "ExtRepl_VolNormalizedTrendDaily"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma_fast"] = out["close"].rolling(self._fast).mean()
        out["sma_slow"] = out["close"].rolling(self._slow).mean()
        out["realized_vol"] = out["close"].pct_change().rolling(self._vol_window).std()
        return out

    def _vol_score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        vol = last.get("realized_vol")
        if vol is None or pd.isna(vol) or vol <= 0:
            return 1.0
        return float(np.clip(self._target_vol / float(vol), 0.25, 1.5))

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if float(last["sma_fast"]) > float(last["sma_slow"]):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=self._vol_score(df),
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if float(last["sma_fast"]) < float(last["sma_slow"]):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "sma_cross_down"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return 0.0
        return self._vol_score(df) if float(last["sma_fast"]) > float(last["sma_slow"]) else 0.0
