"""Autonomous continuous Strategy Discovery campaign (single runner, no cycles).

Reuses existing infrastructure instead of building parallel plumbing:
- data/cache_minute_bars/ + DATASET_HASH + load_or_build_minute_bars() and
  add_microstructure_features() from discover_microstructure_aggtrades.py
  (aggTrades -> 1-minute microstructure bars, hash-verified cache).
- classify_market_regimes() from research/services/market_regime_router_phase18.py.
- _entry_metrics(), _passes_entry_gate(), ENTRY_* gate constants, BASE_FEE,
  STRESS_FEE, _bootstrap() from strategy_discovery_cycle1.py (identical
  scientific gate used by every prior Discovery cycle).
- REJECTED_ENTRY_FAMILIES registry convention (extended here, not replaced).

Adds only what does not exist yet:
- 5 new microstructure features/hypotheses (FLOW_EXHAUSTION, TRADE_SIZE_SHIFT,
  ARRIVAL_RATE_SHOCK, PRICE_FLOW_DIVERGENCE, LARGE_TRADE_CONCENTRATION), each
  genuinely distinct from the 5 already-rejected families.
- A persistent JSON registry (restart-safe: completed hypotheses/configs are
  never re-run) and a process lock (same pattern as collect_aggtrades.py).
- A single continuous campaign loop with cost-stress + light robustness
  checks, stopping at 2 CANDIDATEs or SEARCH_SPACE_EXHAUSTED.

Does not touch BacktestEngine, RiskManager, PositionSizer or Paper Live.
No strategy is registered or deployed from this script.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.services.market_regime_router_phase18 import classify_market_regimes
from discover_microstructure_aggtrades import (
    DATASET_HASH,
    DEV_END,
    SYMBOLS,
    VALIDATION_END,
    add_microstructure_features,
    load_or_build_minute_bars,
)
from strategy_discovery_cycle1 import (
    BASE_FEE,
    ENTRY_FORWARD_HORIZONS,
    ENTRY_MIN_EFFECT_BPS,
    ENTRY_MIN_EPISODES,
    ENTRY_MIN_T_STAT,
    STRESS_FEE,
    _bootstrap,
    _entry_metrics,
    _passes_entry_gate,
)

BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = BASE_DIR / "autonomous_discovery_registry.json"
LOCK_PATH = BASE_DIR / "autonomous_discovery.lock"
OUT_JSON = BASE_DIR / "autonomous_discovery_latest.json"

TARGET_CANDIDATES = 2
SLIPPAGE_BPS = 2.0  # extra one-way stress cost on top of STRESS_FEE

# Already exhausted in prior sessions -- never re-tested here (any threshold).
REJECTED_FAMILIES = {
    "BLOCK_FLOW_SIGNAL",
    "BURST_PERSISTENCE",
    "CVD_ACCELERATION",
    "FLOW_ABSORPTION",
    "TAKER_FLOW_IMBALANCE",
}


def _log(message: str) -> None:
    print(message, flush=True)


def _acquire_lock() -> None:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = LOCK_PATH.read_text(encoding="utf-8").strip() if LOCK_PATH.exists() else "?"
        raise RuntimeError(f"Lock {LOCK_PATH} exists (pid={existing}). Another campaign instance may be running.")
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _zscore(series: pd.Series, window: int = 30) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean().shift(1)
    std = series.rolling(window, min_periods=window).std().shift(1)
    return ((series - mean) / std).replace([np.inf, -np.inf], np.nan)


def add_extended_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Adds 5 new, genuinely distinct microstructure signals on top of the
    already-cached bars (which include signed_volume, cvd, intensity_z,
    avg_trade_size, trade_count, max_aggressor_run from
    discover_microstructure_aggtrades.add_microstructure_features)."""
    df = frame.copy()
    sign_flow = np.sign(df["signed_volume"])

    # FLOW_EXHAUSTION: a recent burst of activity (last 3 bars) followed by
    # the current bar going quiet again. Distinct from BURST_PERSISTENCE
    # (which fires DURING the burst); this fires AFTER it fades.
    burst_recent = df["intensity_z"].rolling(3, min_periods=3).max().shift(1)
    quiet_now = df["intensity_z"] < 0
    flow_dir_recent = np.sign(df["signed_volume"].rolling(3, min_periods=3).sum().shift(1))
    df["flow_exhaustion_signal"] = burst_recent.where(quiet_now) * flow_dir_recent

    # TRADE_SIZE_SHIFT: z-scored average trade size alone (distribution
    # shift), signed by current aggressor direction. Distinct from
    # BLOCK_FLOW_SIGNAL (imbalance * size ratio, already rejected).
    size_z = _zscore(df["avg_trade_size"])
    df["trade_size_shift_signal"] = size_z * sign_flow

    # ARRIVAL_RATE_SHOCK: activity-intensity shock signed by recent PRICE
    # momentum (not by order flow), a mechanically different pairing than
    # BURST_PERSISTENCE (signed by flow + run-length).
    price_mom_dir = np.sign(df["close"].pct_change(3))
    df["arrival_rate_shock_signal"] = df["intensity_z"] * price_mom_dir

    # PRICE_FLOW_DIVERGENCE: price return and order flow moving in opposite
    # directions over the same 10-bar window; signal takes the flow's
    # sign/magnitude when it contradicts price (flow leads, price catches up).
    price_ret_z = _zscore(df["close"].pct_change(10))
    flow_sum_z = _zscore(df["signed_volume"].rolling(10, min_periods=10).sum())
    opposite = np.sign(price_ret_z) != np.sign(flow_sum_z)
    df["price_flow_divergence_signal"] = flow_sum_z.where(opposite)

    # LARGE_TRADE_CONCENTRATION: unusually large average trade size while
    # trade COUNT is below its trailing average (few, large trades rather
    # than many small ones), signed by aggressor direction.
    trade_count_z = _zscore(df["trade_count"])
    few_trades = trade_count_z < 0
    df["large_trade_concentration_signal"] = size_z.where(few_trades) * sign_flow
    return df


@dataclass(frozen=True)
class HypothesisConfig:
    family: str
    feature: str
    threshold: float
    direction: str  # "long_above" or "long_below"

    @property
    def key(self) -> str:
        return f"{self.family}|{self.feature}|{self.threshold}|{self.direction}"


# Pre-registered, finite search space for this campaign round (not scanned
# by outcome). Two a priori thresholds per direction per family, following
# the same non-outcome-driven calibration used for FLOW_ABSORPTION.
PENDING_HYPOTHESES: list[HypothesisConfig] = []
for _family, _feature in (
    ("FLOW_EXHAUSTION", "flow_exhaustion_signal"),
    ("TRADE_SIZE_SHIFT", "trade_size_shift_signal"),
    ("ARRIVAL_RATE_SHOCK", "arrival_rate_shock_signal"),
    ("PRICE_FLOW_DIVERGENCE", "price_flow_divergence_signal"),
    ("LARGE_TRADE_CONCENTRATION", "large_trade_concentration_signal"),
):
    for _threshold in (1.0, 1.5):
        PENDING_HYPOTHESES.append(HypothesisConfig(_family, _feature, _threshold, "long_above"))
        PENDING_HYPOTHESES.append(HypothesisConfig(_family, _feature, -_threshold, "long_below"))


def _split_bounds(frame: pd.DataFrame, split: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = frame.index.min()
    end = frame.index.max()
    if split == "DEV":
        return start, DEV_END
    if split == "VALIDATION":
        return DEV_END, VALIDATION_END
    if split == "OOS":
        return VALIDATION_END, end + pd.Timedelta(minutes=1)
    raise ValueError(split)


def _raw_episode_records(
    frame: pd.DataFrame, config: HypothesisConfig, split: str, symbol: str, regimes: pd.DataFrame
) -> list[dict[str, Any]]:
    split_start, split_end = _split_bounds(frame, split)
    eligible = (frame.index >= split_start) & (frame.index < split_end)
    if eligible.sum() <= 100:
        return []
    feature = frame[config.feature]
    entry = (feature > config.threshold) if config.direction == "long_above" else (feature < config.threshold)
    entry = entry.fillna(False)
    starts = entry & ~entry.shift(fill_value=False)

    records: list[dict[str, Any]] = []
    n = len(frame)
    warmup = 60
    direction_sign = 1.0 if config.direction == "long_above" else -1.0
    for signal_position in np.flatnonzero(starts.to_numpy(bool)):
        if signal_position < warmup or not bool(eligible[signal_position]):
            continue
        entry_position = signal_position + 1
        if entry_position >= n or not bool(eligible[entry_position]):
            continue
        entry_price = float(frame.iloc[entry_position]["open"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        ts = frame.index[signal_position]
        regime_key = str(regimes.loc[ts, "regime_key"]) if ts in regimes.index else "unknown"
        for horizon in ENTRY_FORWARD_HORIZONS:
            exit_position = entry_position + horizon - 1
            if exit_position >= n or not bool(eligible[exit_position]):
                continue
            path = frame.iloc[entry_position: exit_position + 1]
            forward_return = direction_sign * (float(path.iloc[-1]["close"]) / entry_price - 1.0)
            if direction_sign > 0:
                mfe = float(path["high"].max()) / entry_price - 1.0
                mae = float(path["low"].min()) / entry_price - 1.0
            else:
                mfe = 1.0 - float(path["low"].min()) / entry_price
                mae = 1.0 - float(path["high"].max()) / entry_price
            records.append(
                {
                    "symbol": symbol, "regime": regime_key, "horizon": horizon,
                    "entry_timestamp": ts, "forward_return": forward_return, "mfe": mfe, "mae": mae,
                }
            )
    return records


def _aggregate_cells(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    df = pd.DataFrame(records)
    keys = ["symbol", "regime", "horizon"]
    out = []
    for key, group in df.groupby(keys, sort=True):
        metrics = _entry_metrics(group["forward_return"], group["mfe"], group["mae"])
        out.append({**dict(zip(keys, key)), **metrics})
    return out


def _cell_records(frame: dict[str, pd.DataFrame], config: HypothesisConfig, split: str, regimes: dict[str, pd.DataFrame], symbol: str, regime: str, horizon: int) -> list[dict[str, Any]]:
    raw = _raw_episode_records(frame[symbol], config, split, symbol, regimes[symbol])
    return [r for r in raw if r["regime"] == regime and r["horizon"] == horizon]


def _robustness_check(records: list[dict[str, Any]]) -> tuple[bool, str]:
    if len(records) < ENTRY_MIN_EPISODES:
        return False, "insufficient_episodes_for_robustness"
    df = pd.DataFrame(records)
    days = df["entry_timestamp"].dt.date.nunique()
    span_days = (df["entry_timestamp"].max() - df["entry_timestamp"].min()).days + 1
    if days < 3 or span_days < 3:
        return False, "episodes_concentrated_in_too_few_days"
    per_day = df.groupby(df["entry_timestamp"].dt.date).size()
    if per_day.max() / len(df) > 0.40:
        return False, "single_day_dominates_episodes"
    pnl = df["forward_return"].to_numpy(float)
    abs_pnl = np.abs(pnl)
    if abs_pnl.sum() > 0 and abs_pnl.max() / abs_pnl.sum() > 0.20:
        return False, "single_episode_dominates_pnl"
    return True, "ok"


def _cost_stress(records: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = np.array([r["forward_return"] for r in records], dtype=float)
    gross_pf = float(pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum())) if (pnl < 0).any() else 999.0
    gross_expectancy = float(pnl.mean())

    net_base = pnl - 2 * BASE_FEE
    net_stress = pnl - 2 * STRESS_FEE - 2 * (SLIPPAGE_BPS / 10_000.0)

    def _pf(values: np.ndarray) -> float:
        wins = values[values > 0].sum()
        losses = abs(values[values < 0].sum())
        return float(wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0)

    return {
        "gross_pf": gross_pf, "gross_expectancy": gross_expectancy,
        "net_pf_base_fee": _pf(net_base), "net_expectancy_base_fee": float(net_base.mean()),
        "net_pf_stress": _pf(net_stress), "net_expectancy_stress": float(net_stress.mean()),
        "trades": len(pnl),
    }


def _load_registry() -> dict[str, Any]:
    if REGISTRY_PATH.exists():
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if registry.get("dataset_hash") == DATASET_HASH:
            return registry
        _log("STATUS: RESTART_WARNING registry dataset_hash mismatch, starting fresh registry")
    return {"dataset_hash": DATASET_HASH, "hypotheses": {}, "candidates": [], "totals": {"hypotheses_tested": 0, "configurations_tested": 0, "cells_evaluated": 0}}


def _save_registry(registry: dict[str, Any]) -> None:
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")


def _fmt_elapsed(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main() -> int:
    _acquire_lock()
    started = time.monotonic()
    try:
        registry = _load_registry()
        _log("STATUS: RUNNING CURRENT_STAGE=LOAD_BARS")
        frames: dict[str, pd.DataFrame] = {}
        regimes: dict[str, pd.DataFrame] = {}
        for symbol in SYMBOLS:
            bars = load_or_build_minute_bars(symbol)
            enriched = add_microstructure_features(bars)
            enriched = add_extended_features(enriched)
            frames[symbol] = enriched
            regimes[symbol] = classify_market_regimes(enriched[["open", "high", "low", "close", "volume"]])
            _log(f"STATUS: RUNNING CURRENT_STAGE=BARS_READY CURRENT_SYMBOL={symbol} BARS={len(bars)}")

        total = len(PENDING_HYPOTHESES)
        candidates = registry["candidates"]
        completed = 0
        rejected = 0

        for index, config in enumerate(PENDING_HYPOTHESES, start=1):
            if config.family in REJECTED_FAMILIES:
                continue
            entry = registry["hypotheses"].get(config.key)
            if entry and entry.get("status") not in (None, "PENDING", "RUNNING"):
                completed += 1
                if str(entry["status"]).startswith("REJECTED"):
                    rejected += 1
                continue
            if len(candidates) >= TARGET_CANDIDATES:
                break

            elapsed = time.monotonic() - started
            _log(
                f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} HYPOTHESIS={index}/{total} "
                f"CURRENT_STAGE=DEV CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES} "
                f"ELAPSED={_fmt_elapsed(elapsed)}"
            )
            registry["hypotheses"][config.key] = {"status": "RUNNING", "family": config.family, "config": {"feature": config.feature, "threshold": config.threshold, "direction": config.direction}}
            _save_registry(registry)

            dev_records_all: list[dict[str, Any]] = []
            for symbol in SYMBOLS:
                dev_records_all.extend(_raw_episode_records(frames[symbol], config, "DEV", symbol, regimes[symbol]))
            dev_cells = _aggregate_cells(dev_records_all)
            registry["totals"]["configurations_tested"] += 1
            registry["totals"]["cells_evaluated"] += len(dev_cells)
            dev_survivors = [c for c in dev_cells if _passes_entry_gate(c)]

            if not dev_survivors:
                registry["hypotheses"][config.key] = {
                    "status": "REJECTED_DEV", "family": config.family,
                    "config": {"feature": config.feature, "threshold": config.threshold, "direction": config.direction},
                    "dev_cells": len(dev_cells), "reason": "no_cell_passed_dev_gate",
                }
                registry["totals"]["hypotheses_tested"] += 1
                _save_registry(registry)
                rejected += 1
                completed += 1
                _log(f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_DEV CELLS={len(dev_cells)}")
                continue

            # Validation on the specific surviving cells only (no recalibration).
            best_survivor = None
            for cell in dev_survivors:
                val_records = _cell_records(frames, config, "VALIDATION", regimes, cell["symbol"], cell["regime"], cell["horizon"])
                val_metrics = _entry_metrics(pd.Series([r["forward_return"] for r in val_records]), pd.Series([r["mfe"] for r in val_records]), pd.Series([r["mae"] for r in val_records])) if val_records else _entry_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float))
                if not _passes_entry_gate(val_metrics):
                    continue
                oos_records = _cell_records(frames, config, "OOS", regimes, cell["symbol"], cell["regime"], cell["horizon"])
                oos_metrics = _entry_metrics(pd.Series([r["forward_return"] for r in oos_records]), pd.Series([r["mfe"] for r in oos_records]), pd.Series([r["mae"] for r in oos_records])) if oos_records else _entry_metrics(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float))
                if not _passes_entry_gate(oos_metrics):
                    continue
                best_survivor = {"dev": cell, "validation": val_metrics, "oos": oos_metrics, "oos_records": oos_records, "val_records": val_records}
                break

            registry["totals"]["hypotheses_tested"] += 1
            if best_survivor is None:
                any_val_pass = any(
                    _passes_entry_gate(_entry_metrics(
                        pd.Series([r["forward_return"] for r in _cell_records(frames, config, "VALIDATION", regimes, c["symbol"], c["regime"], c["horizon"])]),
                        pd.Series([r["mfe"] for r in _cell_records(frames, config, "VALIDATION", regimes, c["symbol"], c["regime"], c["horizon"])]),
                        pd.Series([r["mae"] for r in _cell_records(frames, config, "VALIDATION", regimes, c["symbol"], c["regime"], c["horizon"])]),
                    ))
                    for c in dev_survivors
                )
                status = "REJECTED_OOS" if any_val_pass else "REJECTED_VALIDATION"
                registry["hypotheses"][config.key] = {
                    "status": status, "family": config.family,
                    "config": {"feature": config.feature, "threshold": config.threshold, "direction": config.direction},
                    "dev_survivors": len(dev_survivors),
                }
                _save_registry(registry)
                rejected += 1
                completed += 1
                _log(f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE={status}")
                continue

            robust_ok, robust_reason = _robustness_check(best_survivor["oos_records"])
            if not robust_ok:
                registry["hypotheses"][config.key] = {
                    "status": "REJECTED_ROBUSTNESS", "family": config.family,
                    "config": {"feature": config.feature, "threshold": config.threshold, "direction": config.direction},
                    "reason": robust_reason,
                }
                _save_registry(registry)
                rejected += 1
                completed += 1
                _log(f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_ROBUSTNESS REASON={robust_reason}")
                continue

            cost = _cost_stress(best_survivor["oos_records"])
            bootstrap = _bootstrap([r["forward_return"] for r in best_survivor["oos_records"]])
            if cost["net_pf_stress"] <= 1.0 or cost["net_expectancy_stress"] <= 0.0:
                registry["hypotheses"][config.key] = {
                    "status": "REJECTED_COSTS", "family": config.family,
                    "config": {"feature": config.feature, "threshold": config.threshold, "direction": config.direction},
                    "cost_stress": cost,
                }
                _save_registry(registry)
                rejected += 1
                completed += 1
                _log(f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_COSTS NET_PF_STRESS={cost['net_pf_stress']:.2f}")
                continue

            candidate = {
                "family": config.family, "signal": config.feature, "threshold": config.threshold, "direction": config.direction,
                "symbol": best_survivor["dev"]["symbol"], "regime": best_survivor["dev"]["regime"], "horizon": best_survivor["dev"]["horizon"],
                "dev": best_survivor["dev"], "validation": best_survivor["validation"], "oos": best_survivor["oos"],
                "cost_stress": cost, "bootstrap": bootstrap,
            }
            candidates.append(candidate)
            registry["candidates"] = candidates
            registry["hypotheses"][config.key] = {"status": "CANDIDATE", "family": config.family, "candidate": candidate}
            _save_registry(registry)
            completed += 1
            _log(f"STATUS: RUNNING CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=CANDIDATE_FOUND CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES}")

        elapsed = time.monotonic() - started
        final_status = "TARGET_REACHED" if len(candidates) >= TARGET_CANDIDATES else "SEARCH_SPACE_EXHAUSTED"
        _log(f"STATUS: {final_status} CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES} ELAPSED={_fmt_elapsed(elapsed)}")
        registry["status"] = final_status
        _save_registry(registry)
        OUT_JSON.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
