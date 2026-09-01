"""Autonomous microstructure research v3.

Goals:
- Reuse validated dataset hash and existing 1m cache (no raw aggTrades reload).
- Keep FINAL_HOLDOUT structurally locked (rows >= oos_end are never loaded).
- Run full funnel per hypothesis:
  DEV -> ENTRY EDGE -> VALIDATION -> OOS -> ROBUSTNESS -> COST STRESS -> OPERATIONAL BACKTEST.
- Preserve strict gates (no relaxation).
- Maintain persistent registry + lock (restart-safe).
- Continue generating economically distinct hypotheses after initial round.
- Stop only when:
  A) TARGET_REACHED_AWAITING_FINAL_HOLDOUT (>=2 distinct candidates), or
  B) RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE (scientific exhaustion criteria met).
"""
from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from research.services.market_regime_router_phase18 import classify_market_regimes
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategy_discovery_cycle1 import (
    BASE_FEE,
    ENTRY_FORWARD_HORIZONS,
    ENTRY_MIN_EPISODES,
    STRESS_FEE,
    _bootstrap,
    _entry_metrics,
    _passes_entry_gate,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "research_pipeline_state.json"
CACHE_ROOT = BASE_DIR / "data" / "cache_minute_bars_v2"
PREV_REGISTRY_PATH = BASE_DIR / "autonomous_discovery_v2_registry.json"
REGISTRY_PATH = BASE_DIR / "autonomous_research_v3_registry.json"
LOCK_PATH = BASE_DIR / "autonomous_research_v3.lock"
LOG_PATH = BASE_DIR / "autonomous_research_v3.log"

TARGET_CANDIDATES = 2
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TIMEFRAME = "1m"
SLIPPAGE_BPS = 2.0

# Same temporal boundaries used by pipeline.
DEV_END = pd.Timestamp("2025-09-01T00:00:00Z")
VALIDATION_END = pd.Timestamp("2026-02-01T00:00:00Z")
OOS_END = pd.Timestamp("2026-06-01T00:00:00Z")

# Scientific search budget controls (explicit anti-data-mining accounting).
MAX_HYPOTHESES = 140
MAX_CONFIGURATIONS = 220
MAX_CONTEXT_CELLS = 60_000


@dataclass(frozen=True)
class HypothesisSpec:
    hypothesis_id: str
    family: str
    economic_rationale: str
    signal_definition: str
    features_used: tuple[str, ...]
    direction: str  # long_above | long_below
    parameters: dict[str, float | int | str]
    expected_mechanism: str

    @property
    def signal_feature(self) -> str:
        return str(self.parameters["signal_feature"])

    @property
    def threshold(self) -> float:
        return float(self.parameters["threshold"])

    @property
    def hold_bars(self) -> int:
        return int(self.parameters.get("hold_bars", 5))


class SignalHoldStrategy(BaseStrategy):
    """Minimal strategy adapter for operational backtest after OOS pass.

    It uses precomputed entry starts from a signal rule and exits after a fixed
    holding horizon; stops/targets remain active via BacktestEngine + RiskManager.
    """

    def __init__(self, frame: pd.DataFrame, feature: str, threshold: float, direction: str, hold_bars: int, name: str) -> None:
        self._frame = frame
        self._feature = feature
        self._threshold = threshold
        self._direction = direction
        self._hold_bars = max(1, int(hold_bars))
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        feature = out[self._feature] if self._feature in out.columns else pd.Series(np.nan, index=out.index)
        if self._direction == "long_above":
            entry = feature > self._threshold
        else:
            entry = feature < self._threshold
        entry = entry.fillna(False)
        starts = entry & ~entry.shift(fill_value=False)

        exit_marks = pd.Series(False, index=out.index)
        start_positions = np.flatnonzero(starts.to_numpy(bool))
        for pos in start_positions:
            exit_pos = pos + self._hold_bars
            if exit_pos < len(out.index):
                exit_marks.iloc[exit_pos] = True

        out["_entry_start"] = starts
        out["_exit_mark"] = exit_marks
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        row = df.iloc[-1]
        px = float(row["close"])
        if bool(row.get("_entry_start", False)):
            # Keep absolute levels valid for RiskManager constraints.
            return StrategySignal(
                signal=SignalType.BUY,
                price=px,
                timestamp=row.name.to_pydatetime(),
                score=1.0,
                stop_loss=px * 0.997,
                take_profit=px * 1.004,
                trailing_stop_pct=0.003,
                metadata={"source": "autonomous_research_v3"},
            )
        return StrategySignal(signal=SignalType.HOLD, price=px, timestamp=row.name.to_pydatetime(), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        row = df.iloc[-1]
        px = float(row["close"])
        if bool(row.get("_exit_mark", False)):
            return StrategySignal(signal=SignalType.SELL, price=px, timestamp=row.name.to_pydatetime(), score=1.0)
        return StrategySignal(signal=SignalType.HOLD, price=px, timestamp=row.name.to_pydatetime(), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        return 1.0


def _log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _acquire_lock() -> None:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = LOCK_PATH.read_text(encoding="utf-8").strip() if LOCK_PATH.exists() else "?"
        raise RuntimeError(f"Lock exists: {LOCK_PATH} pid={existing}")
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _load_pipeline_context() -> tuple[str, Path]:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    dataset_hash = str(state.get("DATASET_MANIFEST_HASH", "")).strip()
    if not dataset_hash:
        # Fallback to newest cache folder.
        folders = [p for p in CACHE_ROOT.iterdir() if p.is_dir()]
        if not folders:
            raise RuntimeError("No cache folder found")
        folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        dataset_hash = folders[0].name
    cache_dir = CACHE_ROOT / dataset_hash
    if not cache_dir.exists():
        raise RuntimeError(f"Cache dir not found for dataset hash: {dataset_hash}")
    return dataset_hash, cache_dir


def _load_bars(cache_dir: Path, symbol: str) -> pd.DataFrame:
    path = cache_dir / f"{symbol}_{TIMEFRAME}.parquet"
    if not path.exists():
        raise RuntimeError(f"Missing cache file: {path}")
    bars = pd.read_parquet(path)
    # FINAL_HOLDOUT structural lock.
    return bars.loc[bars.index < OOS_END].copy()


def _split_bounds(frame: pd.DataFrame, split: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = frame.index.min()
    if split == "DEV":
        return start, DEV_END
    if split == "VALIDATION":
        return DEV_END, VALIDATION_END
    if split == "OOS":
        return VALIDATION_END, OOS_END
    raise ValueError(split)


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean().shift(1)
    std = series.rolling(window, min_periods=window).std().shift(1)
    return ((series - mean) / std).replace([np.inf, -np.inf], np.nan)


def _add_features(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    for symbol, df in frames.items():
        ret1 = df["close"].pct_change()
        ret5 = df["close"].pct_change(5)
        range_hl = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        flow_imb = df["signed_volume"] / df["volume"].replace(0, np.nan)

        df["ret1"] = ret1
        df["ret5"] = ret5
        df["ret10"] = df["close"].pct_change(10)
        df["range_hl"] = range_hl
        df["flow_imb"] = flow_imb
        df["flow_imb_z30"] = _zscore(flow_imb, 30)
        df["trade_intensity_z30"] = _zscore(df["trade_count"], 30)
        df["avg_trade_size_z30"] = _zscore(df["avg_trade_size"], 30)
        df["price_move_z30"] = _zscore(ret1.abs(), 30)

        # 1) FLOW REVERSAL
        df["flow_reversal_signal"] = (-flow_imb.shift(1) * ret1).replace(0.0, np.nan)
        # 2) FLOW CONTINUATION
        df["flow_continuation_signal"] = (flow_imb.rolling(5, min_periods=5).mean() * ret5).replace(0.0, np.nan)
        # 3) ABSORPTION + RELEASE
        df["absorption_release_signal"] = ((df["flow_imb_z30"].abs() - df["price_move_z30"]) * ret1.shift(-1)).replace(0.0, np.nan)
        # 4) FAILED AUCTION / BREAKOUT
        prior_high = df["high"].rolling(20, min_periods=20).max().shift(1)
        prior_low = df["low"].rolling(20, min_periods=20).min().shift(1)
        breakout_up_fail = (df["high"] > prior_high) & (df["close"] < prior_high)
        breakout_dn_fail = (df["low"] < prior_low) & (df["close"] > prior_low)
        df["failed_breakout_signal"] = np.where(breakout_up_fail, -1.0, np.where(breakout_dn_fail, 1.0, np.nan))
        # 5) PRICE IMPACT EFFICIENCY
        df["impact_efficiency_signal"] = (ret1 / (flow_imb.abs() + 1e-9)).replace([np.inf, -np.inf], np.nan)
        # 6) PRICE IMPACT DECAY
        shock = _zscore(flow_imb.abs(), 30)
        df["impact_decay_signal"] = (shock * ret1.shift(3)).replace(0.0, np.nan)
        # 7) TRADE INTENSITY x PRICE RESPONSE
        df["intensity_price_response_signal"] = (df["trade_intensity_z30"] * _zscore(ret1, 30)).replace(0.0, np.nan)
        # 8) LARGE TRADE FOLLOW-THROUGH
        df["large_trade_follow_signal"] = (df["avg_trade_size_z30"] * ret5).replace(0.0, np.nan)
        # 9) MICROSTRUCTURE MOMENTUM
        df["micro_momentum_signal"] = (flow_imb.rolling(8, min_periods=8).mean() + ret5 + 0.2 * df["trade_intensity_z30"]).replace(0.0, np.nan)
        # 10) MICROSTRUCTURE MEAN REVERSION
        df["micro_mean_reversion_signal"] = (flow_imb - ret1).replace(0.0, np.nan)
        # 11) VOL EXPANSION AFTER FLOW COMPRESSION
        flow_compress = flow_imb.rolling(20, min_periods=20).std()
        vol_expand = range_hl.rolling(10, min_periods=10).mean() / range_hl.rolling(40, min_periods=40).mean()
        df["flow_compression_vol_expansion_signal"] = ((-flow_compress) * vol_expand).replace([np.inf, -np.inf], np.nan)

        regimes = classify_market_regimes(df[["open", "high", "low", "close", "volume"]])
        df["regime_key"] = regimes["regime_key"].reindex(df.index)
        trend = df["regime_key"].str.split("|").str[0]
        trend_sign = trend.map({"bullish": 1.0, "bearish": -1.0, "sideways": 0.0})
        changed = df["regime_key"] != df["regime_key"].shift(1)
        # 15) REGIME TRANSITION SIGNALS
        df["regime_transition_signal"] = (trend_sign * changed.astype(float)).replace(0.0, np.nan)

    btc = frames["BTCUSDT"]
    eth = frames["ETHUSDT"]
    common = btc.index.intersection(eth.index)

    # 12/13) BTC <-> ETH information lead
    btc_lead = _zscore(btc.loc[common, "ret1"] + btc.loc[common, "flow_imb"], 30)
    eth_lead = _zscore(eth.loc[common, "ret1"] + eth.loc[common, "flow_imb"], 30)
    frames["ETHUSDT"].loc[common, "btc_to_eth_lead_signal"] = btc_lead
    frames["BTCUSDT"].loc[common, "eth_to_btc_lead_signal"] = eth_lead

    # 14) CROSS-ASSET FLOW DIVERGENCE
    divergence = (btc.loc[common, "flow_imb"] - eth.loc[common, "flow_imb"]) - (btc.loc[common, "ret1"] - eth.loc[common, "ret1"])
    frames["BTCUSDT"].loc[common, "cross_asset_flow_divergence_signal"] = divergence
    frames["ETHUSDT"].loc[common, "cross_asset_flow_divergence_signal"] = -divergence

    return frames


def _feature_quantiles(frames: dict[str, pd.DataFrame], feature: str) -> tuple[float, float]:
    values = []
    for symbol in SYMBOLS:
        df = frames[symbol]
        if feature not in df.columns:
            continue
        start, end = _split_bounds(df, "DEV")
        part = df.loc[(df.index >= start) & (df.index < end), feature].dropna()
        if len(part):
            values.append(part)
    if not values:
        return (0.5, -0.5)
    joined = pd.concat(values)
    q_hi = float(joined.quantile(0.8))
    q_lo = float(joined.quantile(0.2))
    if q_hi == q_lo:
        q_hi += 1e-6
        q_lo -= 1e-6
    return (q_hi, q_lo)


def _raw_episode_records(frame: pd.DataFrame, spec: HypothesisSpec, split: str, symbol: str) -> list[dict[str, Any]]:
    split_start, split_end = _split_bounds(frame, split)
    eligible = pd.Series((frame.index >= split_start) & (frame.index < split_end), index=frame.index)
    if eligible.sum() <= 100 or spec.signal_feature not in frame.columns:
        return []

    signal = frame[spec.signal_feature]
    entry = signal > spec.threshold if spec.direction == "long_above" else signal < spec.threshold
    entry = entry.fillna(False)
    starts = entry & ~entry.shift(fill_value=False)

    records: list[dict[str, Any]] = []
    n = len(frame)
    warmup = 60
    direction_sign = 1.0 if spec.direction == "long_above" else -1.0

    for signal_position in np.flatnonzero(starts.to_numpy(bool)):
        if signal_position < warmup or not bool(eligible.iloc[signal_position]):
            continue
        entry_position = signal_position + 1
        if entry_position >= n or not bool(eligible.iloc[entry_position]):
            continue
        entry_price = float(frame.iloc[entry_position]["open"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        ts = frame.index[signal_position]
        regime_key = str(frame["regime_key"].iloc[signal_position]) if "regime_key" in frame.columns else "unknown"

        for horizon in ENTRY_FORWARD_HORIZONS:
            exit_position = entry_position + horizon - 1
            if exit_position >= n or not bool(eligible.iloc[exit_position]):
                continue
            path = frame.iloc[entry_position : exit_position + 1]
            forward_return = direction_sign * (float(path.iloc[-1]["close"]) / entry_price - 1.0)
            if direction_sign > 0:
                mfe = float(path["high"].max()) / entry_price - 1.0
                mae = float(path["low"].min()) / entry_price - 1.0
            else:
                mfe = 1.0 - float(path["low"].min()) / entry_price
                mae = 1.0 - float(path["high"].max()) / entry_price
            records.append(
                {
                    "symbol": symbol,
                    "regime": regime_key,
                    "horizon": horizon,
                    "entry_timestamp": ts,
                    "forward_return": forward_return,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
    return records


def _aggregate_cells(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    df = pd.DataFrame(records)
    out = []
    for key, group in df.groupby(["symbol", "regime", "horizon"], sort=True):
        metrics = _entry_metrics(group["forward_return"], group["mfe"], group["mae"])
        out.append({"symbol": key[0], "regime": key[1], "horizon": int(key[2]), **metrics})
    return out


def _robustness_check(records: list[dict[str, Any]]) -> tuple[bool, str]:
    if len(records) < ENTRY_MIN_EPISODES:
        return False, "insufficient_episodes_for_robustness"
    df = pd.DataFrame(records)
    days = df["entry_timestamp"].dt.date.nunique()
    if days < 10:
        return False, "episodes_concentrated_in_too_few_days"
    per_day = df.groupby(df["entry_timestamp"].dt.date).size()
    if per_day.max() / len(df) > 0.25:
        return False, "single_day_dominates_episodes"
    pnl = df["forward_return"].to_numpy(float)
    abs_pnl = np.abs(pnl)
    if abs_pnl.sum() > 0 and abs_pnl.max() / abs_pnl.sum() > 0.10:
        return False, "single_episode_dominates_pnl"
    return True, "ok"


def _cost_stress(records: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = np.array([r["forward_return"] for r in records], dtype=float)

    def _pf(values: np.ndarray) -> float:
        wins = values[values > 0].sum()
        losses = abs(values[values < 0].sum())
        return float(wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0)

    net_base = pnl - 2 * BASE_FEE
    net_stress = pnl - 2 * STRESS_FEE - 2 * (SLIPPAGE_BPS / 10_000.0)
    return {
        "gross_pf": _pf(pnl),
        "gross_expectancy": float(pnl.mean()) if len(pnl) else 0.0,
        "net_pf_base_fee": _pf(net_base),
        "net_expectancy_base_fee": float(net_base.mean()) if len(net_base) else 0.0,
        "net_pf_stress": _pf(net_stress),
        "net_expectancy_stress": float(net_stress.mean()) if len(net_stress) else 0.0,
        "trades": int(len(pnl)),
    }


def _operational_backtest(frame: pd.DataFrame, spec: HypothesisSpec, selected: dict[str, Any]) -> dict[str, Any]:
    symbol = str(selected["symbol"])
    horizon = int(selected["horizon"])
    split_start, split_end = _split_bounds(frame, "OOS")
    oos_df = frame.loc[(frame.index >= split_start) & (frame.index < split_end), ["open", "high", "low", "close", "volume"]].copy()

    strategy = SignalHoldStrategy(
        frame=frame,
        feature=spec.signal_feature,
        threshold=spec.threshold,
        direction=spec.direction,
        hold_bars=max(1, horizon),
        name=f"ResearchV3_{spec.family}",
    )
    strategy.initialize()
    engine = BacktestEngine(strategy, BacktestConfig(initial_capital=10_000.0, fee_pct=BASE_FEE, warmup_bars=60))
    result = engine.run(oos_df, symbol=symbol, timeframe=TIMEFRAME)
    m = result.metrics
    return {
        "total_trades": int(m.total_trades),
        "profit_factor": float(m.profit_factor),
        "expectancy": float(m.expectancy),
        "net_profit": float(m.net_profit),
        "max_drawdown_pct": float(m.max_drawdown_pct),
        "sharpe_ratio": float(m.sharpe_ratio),
    }


def _parse_previous_registry() -> list[dict[str, Any]]:
    if not PREV_REGISTRY_PATH.exists():
        return []
    data = json.loads(PREV_REGISTRY_PATH.read_text(encoding="utf-8"))
    out = []
    for key, node in data.get("hypotheses", {}).items():
        parts = key.split("|")
        if len(parts) != 4:
            continue
        out.append(
            {
                "hypothesis_key": key,
                "family": parts[0],
                "signal": parts[1],
                "threshold": float(parts[2]),
                "direction": parts[3],
                "rejection_stage": node.get("status", "UNKNOWN"),
                "rejection_reason": node.get("status", "UNKNOWN"),
            }
        )
    return out


def _initial_registry(dataset_hash: str, previous: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dataset_manifest_hash": dataset_hash,
        "timeframe": TIMEFRAME,
        "target_candidates": TARGET_CANDIDATES,
        "final_holdout_locked": True,
        "previous_rejections": previous,
        "hypotheses": {},
        "candidates": [],
        "totals": {
            "TOTAL_HYPOTHESES_TESTED": 0,
            "TOTAL_CONFIGURATIONS_TESTED": 0,
            "TOTAL_CONTEXT_CELLS_TESTED": 0,
            "DEV_SURVIVORS": 0,
            "VALIDATION_SURVIVORS": 0,
            "OOS_SURVIVORS": 0,
        },
        "status": "RUNNING",
        "round": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_registry(dataset_hash: str) -> dict[str, Any]:
    if REGISTRY_PATH.exists():
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if reg.get("dataset_manifest_hash") == dataset_hash:
            return reg
    previous = _parse_previous_registry()
    return _initial_registry(dataset_hash, previous)


def _save_registry(reg: dict[str, Any]) -> None:
    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, default=str), encoding="utf-8")


def _equivalence_key(spec: HypothesisSpec) -> str:
    return (
        f"{spec.family}|{spec.signal_feature}|{spec.direction}|"
        f"{round(spec.threshold, 6)}|{spec.parameters.get('window', '-') }|{spec.expected_mechanism}"
    )


def _already_tested_equivalents(reg: dict[str, Any]) -> set[str]:
    eq = set()
    for key, node in reg.get("hypotheses", {}).items():
        sig = node.get("equivalence_key")
        if sig:
            eq.add(str(sig))
    for prev in reg.get("previous_rejections", []):
        eq.add(
            f"{prev.get('family')}|{prev.get('signal')}|{prev.get('direction')}|"
            f"{round(float(prev.get('threshold', 0.0)), 6)}|-|legacy"
        )
    return eq


def _make_hypothesis(h_id: int, family: str, rationale: str, signal_def: str, feature: str, direction: str, threshold: float, mechanism: str, extra: dict[str, Any] | None = None) -> HypothesisSpec:
    params: dict[str, float | int | str] = {
        "signal_feature": feature,
        "threshold": float(threshold),
        "hold_bars": int((extra or {}).get("hold_bars", 5)),
        "window": int((extra or {}).get("window", 20)),
    }
    return HypothesisSpec(
        hypothesis_id=f"H{h_id:04d}_{family}_{direction}",
        family=family,
        economic_rationale=rationale,
        signal_definition=signal_def,
        features_used=(feature,),
        direction=direction,
        parameters=params,
        expected_mechanism=mechanism,
    )


def _generate_round(frames: dict[str, pd.DataFrame], round_idx: int, reg: dict[str, Any]) -> list[HypothesisSpec]:
    next_id = 1 + len(reg.get("hypotheses", {}))
    out: list[HypothesisSpec] = []

    seeds = [
        ("FLOW_REVERSAL", "flow_reversal_signal", "abrupt aggressor dominance reversal", "reversal"),
        ("FLOW_CONTINUATION", "flow_continuation_signal", "persistent aggressive flow with efficient price response", "continuation"),
        ("ABSORPTION_RELEASE", "absorption_release_signal", "aggressive flow absorbed then released", "absorption_release"),
        ("FAILED_BREAKOUT", "failed_breakout_signal", "aggressive break of recent extremes followed by snap-back", "failed_auction"),
        ("IMPACT_EFFICIENCY", "impact_efficiency_signal", "price displacement per aggressive volume unit", "impact_efficiency"),
        ("IMPACT_DECAY", "impact_decay_signal", "post-shock impact persistence or reversal", "impact_decay"),
        ("INTENSITY_X_RESPONSE", "intensity_price_response_signal", "trade frequency conditioned by move efficiency", "intensity_response"),
        ("LARGE_TRADE_FOLLOW", "large_trade_follow_signal", "large trade concentration followed by continuation/reversal", "large_trade"),
        ("MICRO_MOMENTUM", "micro_momentum_signal", "combined aggression+intensity+price response", "micro_momentum"),
        ("MICRO_MEAN_REVERSION", "micro_mean_reversion_signal", "extreme imbalance without proportional displacement", "micro_mean_reversion"),
        ("FLOW_COMPRESSION_VOL_EXPANSION", "flow_compression_vol_expansion_signal", "compression then volatility expansion", "vol_expansion"),
        ("BTC_TO_ETH_LEAD", "btc_to_eth_lead_signal", "BTC information leads ETH", "cross_asset_lead"),
        ("ETH_TO_BTC_LEAD", "eth_to_btc_lead_signal", "ETH information leads BTC", "cross_asset_lead"),
        ("CROSS_ASSET_FLOW_DIVERGENCE", "cross_asset_flow_divergence_signal", "abnormal BTC/ETH flow-price divergence", "cross_asset_divergence"),
        ("REGIME_TRANSITION_EVENT", "regime_transition_signal", "signals near regime transitions, not universal filter", "regime_transition"),
    ]

    eq_existing = _already_tested_equivalents(reg)

    # Round 1: two quantile thresholds per feature (dev only), both directions.
    # Round 2+: adaptive widening around DEV quantiles (still bounded, no brute force).
    for family, feature, rationale, mechanism in seeds:
        q_hi, q_lo = _feature_quantiles(frames, feature)
        if round_idx == 1:
            thresholds = [("long_above", q_hi), ("long_below", q_lo)]
        else:
            span = max(abs(q_hi), abs(q_lo), 1e-6)
            thresholds = [
                ("long_above", q_hi + 0.25 * span),
                ("long_below", q_lo - 0.25 * span),
            ]
        for direction, thr in thresholds:
            spec = _make_hypothesis(
                h_id=next_id,
                family=family,
                rationale=rationale,
                signal_def=f"{feature} with {direction} threshold={thr:.6f} (DEV quantile-based)",
                feature=feature,
                direction=direction,
                threshold=float(thr),
                mechanism=mechanism,
                extra={"hold_bars": 5 if mechanism != "cross_asset_lead" else 8, "window": 20},
            )
            legacy_key = f"{family}|{feature}|{direction}|{round(float(thr), 6)}|-|legacy"
            if _equivalence_key(spec) in eq_existing or legacy_key in eq_existing:
                continue
            out.append(spec)
            next_id += 1

    return out


def _scientific_exhausted(reg: dict[str, Any], round_idx: int) -> bool:
    totals = reg["totals"]
    tested = int(totals["TOTAL_HYPOTHESES_TESTED"])
    dev_surv = int(totals["DEV_SURVIVORS"])
    val_surv = int(totals["VALIDATION_SURVIVORS"])
    oos_surv = int(totals["OOS_SURVIVORS"])

    if tested >= MAX_HYPOTHESES:
        return True
    if totals["TOTAL_CONFIGURATIONS_TESTED"] >= MAX_CONFIGURATIONS:
        return True
    if totals["TOTAL_CONTEXT_CELLS_TESTED"] >= MAX_CONTEXT_CELLS:
        return True

    # After at least 2 adaptive rounds and enough tests, no temporal generalization.
    if round_idx >= 2 and tested >= 40 and dev_surv > 0 and val_surv == 0 and oos_surv == 0:
        return True

    # Stronger criterion: no DEV survivor at all after broad mechanism coverage.
    if round_idx >= 2 and tested >= 30 and dev_surv == 0:
        return True

    return False


def run() -> int:
    _acquire_lock()
    started = time.monotonic()
    try:
        dataset_hash, cache_dir = _load_pipeline_context()
        registry = _load_registry(dataset_hash)

        frames = {symbol: _load_bars(cache_dir, symbol) for symbol in SYMBOLS}
        frames = _add_features(frames)

        candidates = registry["candidates"]
        _log(
            "AUTONOMOUS_RESEARCH_STARTED: YES "
            f"PID: {os.getpid()} "
            f"DATASET_MANIFEST_HASH: {dataset_hash} "
            "CACHE_VALID: YES "
            "FINAL_HOLDOUT_LOCKED: YES"
        )

        if len(candidates) >= TARGET_CANDIDATES:
            registry["status"] = "TARGET_REACHED_AWAITING_FINAL_HOLDOUT"
            _save_registry(registry)
            _log("CURRENT_STAGE: TARGET_REACHED_AWAITING_FINAL_HOLDOUT")
            return 0

        round_idx = int(registry.get("round", 0))

        while True:
            round_idx += 1
            registry["round"] = round_idx
            hypothesis_batch = _generate_round(frames, round_idx, registry)
            _save_registry(registry)

            if not hypothesis_batch:
                if _scientific_exhausted(registry, round_idx):
                    registry["status"] = "RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE"
                    _save_registry(registry)
                    _log("STAGE: RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE")
                    return 0
                # No novel non-equivalent hypotheses in this round; continue one more adaptive round.
                if round_idx >= 3:
                    registry["status"] = "RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE"
                    _save_registry(registry)
                    _log("STAGE: RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE")
                    return 0
                continue

            total_batch = len(hypothesis_batch)
            for idx, spec in enumerate(hypothesis_batch, start=1):
                if len(candidates) >= TARGET_CANDIDATES:
                    registry["status"] = "TARGET_REACHED_AWAITING_FINAL_HOLDOUT"
                    _save_registry(registry)
                    _log("STAGE: TARGET_REACHED_AWAITING_FINAL_HOLDOUT")
                    return 0

                if spec.hypothesis_id in registry["hypotheses"]:
                    continue

                elapsed = time.monotonic() - started
                tested = int(registry["totals"]["TOTAL_HYPOTHESES_TESTED"])
                remaining = max(total_batch - idx + 1, 1)
                rate = tested / elapsed if elapsed > 0 and tested > 0 else 0.0
                eta = remaining / rate if rate > 0 else 0.0

                try:
                    registry["totals"]["TOTAL_HYPOTHESES_TESTED"] += 1
                    registry["totals"]["TOTAL_CONFIGURATIONS_TESTED"] += 1

                    dev_records = []
                    for symbol in SYMBOLS:
                        dev_records.extend(_raw_episode_records(frames[symbol], spec, "DEV", symbol))
                    dev_cells = _aggregate_cells(dev_records)
                    registry["totals"]["TOTAL_CONTEXT_CELLS_TESTED"] += len(dev_cells)
                    dev_survivors = [cell for cell in dev_cells if _passes_entry_gate(cell)]
                    if dev_survivors:
                        registry["totals"]["DEV_SURVIVORS"] += 1

                    status = "REJECTED_DEV"
                    details: dict[str, Any] = {
                        "hypothesis": {
                            "HYPOTHESIS_ID": spec.hypothesis_id,
                            "FAMILY": spec.family,
                            "ECONOMIC_RATIONALE": spec.economic_rationale,
                            "SIGNAL_DEFINITION": spec.signal_definition,
                            "FEATURES_USED": list(spec.features_used),
                            "DIRECTION": spec.direction,
                            "PARAMETERS": spec.parameters,
                            "EXPECTED_MECHANISM": spec.expected_mechanism,
                        },
                        "equivalence_key": _equivalence_key(spec),
                        "stage": "DEV",
                        "dev_cells": len(dev_cells),
                        "dev_survivors": len(dev_survivors),
                    }

                    validation_survivors = 0
                    oos_survivors = 0

                    if dev_survivors:
                        selected = None
                        for cell in dev_survivors:
                            val_records = [
                                r
                                for r in _raw_episode_records(frames[cell["symbol"]], spec, "VALIDATION", cell["symbol"])
                                if r["regime"] == cell["regime"] and r["horizon"] == cell["horizon"]
                            ]
                            if not val_records:
                                continue
                            val_metrics = _entry_metrics(
                                pd.Series([r["forward_return"] for r in val_records]),
                                pd.Series([r["mfe"] for r in val_records]),
                                pd.Series([r["mae"] for r in val_records]),
                            )
                            if not _passes_entry_gate(val_metrics):
                                continue

                            validation_survivors += 1
                            oos_records = [
                                r
                                for r in _raw_episode_records(frames[cell["symbol"]], spec, "OOS", cell["symbol"])
                                if r["regime"] == cell["regime"] and r["horizon"] == cell["horizon"]
                            ]
                            if not oos_records:
                                continue
                            oos_metrics = _entry_metrics(
                                pd.Series([r["forward_return"] for r in oos_records]),
                                pd.Series([r["mfe"] for r in oos_records]),
                                pd.Series([r["mae"] for r in oos_records]),
                            )
                            if not _passes_entry_gate(oos_metrics):
                                continue

                            oos_survivors += 1
                            robust_ok, robust_reason = _robustness_check(oos_records)
                            if not robust_ok:
                                status = "REJECTED_ROBUSTNESS"
                                details["robustness_reason"] = robust_reason
                                continue

                            cost = _cost_stress(oos_records)
                            if cost["net_pf_stress"] <= 1.0 or cost["net_expectancy_stress"] <= 0.0:
                                status = "REJECTED_COSTS"
                                details["cost_stress"] = cost
                                continue

                            op = _operational_backtest(frames[cell["symbol"]], spec, cell)
                            if op["profit_factor"] <= 1.0 or op["expectancy"] <= 0.0 or op["total_trades"] < 30:
                                status = "REJECTED_OPERATIONAL_BACKTEST"
                                details["operational_backtest"] = op
                                continue

                            selected = {
                                "cell": cell,
                                "validation": val_metrics,
                                "oos": oos_metrics,
                                "bootstrap": _bootstrap([r["forward_return"] for r in oos_records]),
                                "cost_stress": cost,
                                "operational_backtest": op,
                            }
                            break

                        if selected is None:
                            status = "REJECTED_VALIDATION_OR_OOS"
                        else:
                            registry["totals"]["VALIDATION_SURVIVORS"] += 1
                            registry["totals"]["OOS_SURVIVORS"] += 1

                            # Diversity rule: require distinct mechanism cluster among candidates.
                            mechanism = spec.expected_mechanism
                            existing_mechanisms = {c.get("expected_mechanism") for c in candidates}
                            if mechanism in existing_mechanisms:
                                status = "REJECTED_INSUFFICIENT_DIVERSITY"
                            else:
                                status = "CANDIDATE"
                                candidate = {
                                    "hypothesis_id": spec.hypothesis_id,
                                    "family": spec.family,
                                    "expected_mechanism": mechanism,
                                    "signal_feature": spec.signal_feature,
                                    "direction": spec.direction,
                                    "threshold": spec.threshold,
                                    "hold_bars": spec.hold_bars,
                                    "selection": selected,
                                }
                                candidates.append(candidate)
                                registry["candidates"] = candidates
                except Exception as exc:  # noqa: BLE001
                    status = "REJECTED_EXECUTION_ERROR"
                    details = {
                        "hypothesis": {
                            "HYPOTHESIS_ID": spec.hypothesis_id,
                            "FAMILY": spec.family,
                            "ECONOMIC_RATIONALE": spec.economic_rationale,
                            "SIGNAL_DEFINITION": spec.signal_definition,
                            "FEATURES_USED": list(spec.features_used),
                            "DIRECTION": spec.direction,
                            "PARAMETERS": spec.parameters,
                            "EXPECTED_MECHANISM": spec.expected_mechanism,
                        },
                        "equivalence_key": _equivalence_key(spec),
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=8),
                    }
                    validation_survivors = 0
                    oos_survivors = 0

                details["status"] = status
                details["stage"] = status
                details["validation_survivors"] = validation_survivors
                details["oos_survivors"] = oos_survivors
                registry["hypotheses"][spec.hypothesis_id] = details
                _save_registry(registry)

                progress = idx / total_batch * 100.0
                _log(
                    f"CURRENT_FAMILY={spec.family} "
                    f"CURRENT_HYPOTHESIS={spec.hypothesis_id} "
                    f"CURRENT_CONFIGURATION={json.dumps(spec.parameters, sort_keys=True)} "
                    f"STAGE={status} "
                    f"PROGRESS={progress:.1f}%({idx}/{total_batch}) "
                    f"HYPOTHESES_TESTED={registry['totals']['TOTAL_HYPOTHESES_TESTED']} "
                    f"CONFIGURATIONS_TESTED={registry['totals']['TOTAL_CONFIGURATIONS_TESTED']} "
                    f"DEV_SURVIVORS={registry['totals']['DEV_SURVIVORS']} "
                    f"VALIDATION_SURVIVORS={registry['totals']['VALIDATION_SURVIVORS']} "
                    f"OOS_SURVIVORS={registry['totals']['OOS_SURVIVORS']} "
                    f"CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES} "
                    f"ELAPSED={elapsed:.0f}s ETA={eta:.0f}s"
                )

            if len(candidates) >= TARGET_CANDIDATES:
                registry["status"] = "TARGET_REACHED_AWAITING_FINAL_HOLDOUT"
                _save_registry(registry)
                _log("STAGE: TARGET_REACHED_AWAITING_FINAL_HOLDOUT")
                return 0

            if _scientific_exhausted(registry, round_idx):
                registry["status"] = "RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE"
                _save_registry(registry)
                _log("STAGE: RESEARCH_SPACE_EXHAUSTED_WITHOUT_EDGE")
                return 0

    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(run())
