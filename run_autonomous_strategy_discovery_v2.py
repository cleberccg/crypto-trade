"""Discovery funnel v2: same scientific protocol as run_autonomous_strategy_discovery.py
(DEV -> Validation -> OOS -> robustness -> cost stress -> CANDIDATE), pointed
at the new multi-year, multi-timeframe feature cache instead of the Jan-2025
single-month dataset. FINAL_HOLDOUT is enforced structurally: rows at/after
oos_end are never loaded into this process at all.

Adds 2 genuinely new hypothesis families (regime transitions; BTC<->ETH
order-flow lead-lag) -- distinct mechanisms from every previously rejected
family. Reuses the exact gate constants/statistics from
strategy_discovery_cycle1.py, same as every prior Discovery cycle.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.services.market_regime_router_phase18 import classify_market_regimes
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
REGISTRY_PATH = BASE_DIR / "autonomous_discovery_v2_registry.json"
LOCK_PATH = BASE_DIR / "autonomous_discovery_v2.lock"

TARGET_CANDIDATES = 2
SLIPPAGE_BPS = 2.0
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TIMEFRAME = "1m"  # regime-transition and cross-asset flow are evaluated at 1m; justified: both mechanisms are defined bar-to-bar.

REJECTED_FAMILIES = {
    "BLOCK_FLOW_SIGNAL", "BURST_PERSISTENCE", "CVD_ACCELERATION", "FLOW_ABSORPTION",
    "TAKER_FLOW_IMBALANCE", "FLOW_EXHAUSTION", "TRADE_SIZE_SHIFT", "ARRIVAL_RATE_SHOCK",
    "PRICE_FLOW_DIVERGENCE", "LARGE_TRADE_CONCENTRATION",
}


def _log(message: str) -> None:
    print(message, flush=True)


def _acquire_lock() -> None:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = LOCK_PATH.read_text(encoding="utf-8").strip() if LOCK_PATH.exists() else "?"
        raise RuntimeError(f"Lock {LOCK_PATH} exists (pid={existing}).")
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


@dataclass(frozen=True)
class HypothesisConfig:
    family: str
    feature: str
    threshold: float
    direction: str

    @property
    def key(self) -> str:
        return f"{self.family}|{self.feature}|{self.threshold}|{self.direction}"


def _load_bars(cache_dir: Path, symbol: str, oos_end: pd.Timestamp) -> pd.DataFrame:
    path = cache_dir / f"{symbol}_{TIMEFRAME}.parquet"
    bars = pd.read_parquet(path)
    # FINAL_HOLDOUT structural protection: rows >= oos_end are simply never
    # loaded past this point in the process.
    return bars.loc[bars.index < oos_end].copy()


def add_v2_features(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    for symbol, df in frames.items():
        regimes = classify_market_regimes(df[["open", "high", "low", "close", "volume"]])
        df["regime_key"] = regimes["regime_key"].reindex(df.index)

    # REGIME_TRANSITION: fires on the bar where regime_key changes vs the
    # previous bar; signed by the direction of the NEW regime's trend bucket
    # (bullish=+1, bearish=-1, sideways -> no signal). Distinct mechanism:
    # discrete state-change detection, not a continuous feature threshold.
    for symbol, df in frames.items():
        trend = df["regime_key"].str.split("|").str[0]
        trend_sign = trend.map({"bullish": 1.0, "bearish": -1.0, "sideways": 0.0})
        changed = df["regime_key"] != df["regime_key"].shift(1)
        df["regime_transition_signal"] = (trend_sign * changed.astype(float)).replace(0.0, np.nan)

    # BTC_ETH_FLOW_LEADLAG: one asset's recent signed-volume burst (z-scored)
    # used to predict the OTHER asset's forward return. Distinct mechanism:
    # cross-asset order-flow spillover, not a single-asset feature.
    if "BTCUSDT" in frames and "ETHUSDT" in frames:
        btc, eth = frames["BTCUSDT"], frames["ETHUSDT"]
        common_index = btc.index.intersection(eth.index)
        btc_flow = btc.loc[common_index, "signed_volume"]
        eth_flow = eth.loc[common_index, "signed_volume"]

        def _zscore(series: pd.Series, window: int = 30) -> pd.Series:
            mean = series.rolling(window, min_periods=window).mean().shift(1)
            std = series.rolling(window, min_periods=window).std().shift(1)
            return ((series - mean) / std).replace([np.inf, -np.inf], np.nan)

        btc_flow_z = _zscore(btc_flow)
        eth_flow_z = _zscore(eth_flow)
        frames["ETHUSDT"].loc[common_index, "btc_eth_leadlag_signal"] = btc_flow_z.reindex(common_index)
        frames["BTCUSDT"].loc[common_index, "btc_eth_leadlag_signal"] = eth_flow_z.reindex(common_index)
    return frames


PENDING_HYPOTHESES: list[HypothesisConfig] = [
    HypothesisConfig("REGIME_TRANSITION", "regime_transition_signal", 0.5, "long_above"),
    HypothesisConfig("REGIME_TRANSITION", "regime_transition_signal", -0.5, "long_below"),
    HypothesisConfig("BTC_ETH_FLOW_LEADLAG", "btc_eth_leadlag_signal", 1.0, "long_above"),
    HypothesisConfig("BTC_ETH_FLOW_LEADLAG", "btc_eth_leadlag_signal", -1.0, "long_below"),
    HypothesisConfig("BTC_ETH_FLOW_LEADLAG", "btc_eth_leadlag_signal", 1.5, "long_above"),
    HypothesisConfig("BTC_ETH_FLOW_LEADLAG", "btc_eth_leadlag_signal", -1.5, "long_below"),
]


def _split_bounds(frame: pd.DataFrame, split: str, dev_end, validation_end, oos_end) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = frame.index.min()
    if split == "DEV":
        return start, dev_end
    if split == "VALIDATION":
        return dev_end, validation_end
    if split == "OOS":
        return validation_end, oos_end
    raise ValueError(split)


def _raw_episode_records(frame: pd.DataFrame, config: HypothesisConfig, split: str, symbol: str, bounds) -> list[dict[str, Any]]:
    split_start, split_end = bounds
    eligible = (frame.index >= split_start) & (frame.index < split_end)
    if eligible.sum() <= 100 or config.feature not in frame.columns:
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
        regime_key = str(frame["regime_key"].iloc[signal_position]) if "regime_key" in frame.columns else "unknown"
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
            records.append({"symbol": symbol, "regime": regime_key, "horizon": horizon, "entry_timestamp": ts, "forward_return": forward_return, "mfe": mfe, "mae": mae})
    return records


def _aggregate_cells(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    df = pd.DataFrame(records)
    keys = ["symbol", "regime", "horizon"]
    return [{**dict(zip(keys, key)), **_entry_metrics(g["forward_return"], g["mfe"], g["mae"])} for key, g in df.groupby(keys, sort=True)]


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
        "gross_pf": _pf(pnl), "gross_expectancy": float(pnl.mean()),
        "net_pf_base_fee": _pf(net_base), "net_expectancy_base_fee": float(net_base.mean()),
        "net_pf_stress": _pf(net_stress), "net_expectancy_stress": float(net_stress.mean()),
        "trades": len(pnl),
    }


def _load_registry(dataset_hash: str) -> dict[str, Any]:
    if REGISTRY_PATH.exists():
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if registry.get("dataset_manifest_hash") == dataset_hash:
            return registry
    return {"dataset_manifest_hash": dataset_hash, "hypotheses": {}, "candidates": [], "totals": {"hypotheses_tested": 0, "configurations_tested": 0, "cells_evaluated": 0}}


def _save_registry(registry: dict[str, Any]) -> None:
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")


def run(cache_dir: Path, dataset_manifest_hash: str, dev_end: pd.Timestamp, validation_end: pd.Timestamp, oos_end: pd.Timestamp) -> None:
    _acquire_lock()
    started = time.monotonic()
    try:
        _log(f"PIPELINE_STAGE: DISCOVERY FINAL_HOLDOUT_LOCKED=YES (rows >= {oos_end.isoformat()} never loaded)")
        registry = _load_registry(dataset_manifest_hash)
        frames = {symbol: _load_bars(cache_dir, symbol, oos_end) for symbol in SYMBOLS}
        frames = add_v2_features(frames)

        total = len(PENDING_HYPOTHESES)
        candidates = registry["candidates"]

        for index, config in enumerate(PENDING_HYPOTHESES, start=1):
            if config.family in REJECTED_FAMILIES:
                continue
            entry = registry["hypotheses"].get(config.key)
            if entry and entry.get("status") not in (None, "PENDING", "RUNNING"):
                continue
            if len(candidates) >= TARGET_CANDIDATES:
                break

            elapsed = time.monotonic() - started
            _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} HYPOTHESIS_PROGRESS={index}/{total} CURRENT_SPLIT=DEV CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES} ELAPSED={elapsed:.0f}s")

            dev_bounds = {s: _split_bounds(frames[s], "DEV", dev_end, validation_end, oos_end) for s in SYMBOLS}
            dev_records_all: list[dict[str, Any]] = []
            for symbol in SYMBOLS:
                dev_records_all.extend(_raw_episode_records(frames[symbol], config, "DEV", symbol, dev_bounds[symbol]))
            dev_cells = _aggregate_cells(dev_records_all)
            registry["totals"]["configurations_tested"] += 1
            registry["totals"]["cells_evaluated"] += len(dev_cells)
            registry["totals"]["hypotheses_tested"] += 1
            dev_survivors = [c for c in dev_cells if _passes_entry_gate(c)]

            if not dev_survivors:
                registry["hypotheses"][config.key] = {"status": "REJECTED_DEV", "family": config.family, "dev_cells": len(dev_cells)}
                _save_registry(registry)
                _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_DEV CELLS={len(dev_cells)}")
                continue

            best_survivor = None
            for cell in dev_survivors:
                val_bounds = _split_bounds(frames[cell["symbol"]], "VALIDATION", dev_end, validation_end, oos_end)
                val_records = [r for r in _raw_episode_records(frames[cell["symbol"]], config, "VALIDATION", cell["symbol"], val_bounds) if r["regime"] == cell["regime"] and r["horizon"] == cell["horizon"]]
                if not val_records:
                    continue
                val_metrics = _entry_metrics(pd.Series([r["forward_return"] for r in val_records]), pd.Series([r["mfe"] for r in val_records]), pd.Series([r["mae"] for r in val_records]))
                if not _passes_entry_gate(val_metrics):
                    continue
                oos_bounds = _split_bounds(frames[cell["symbol"]], "OOS", dev_end, validation_end, oos_end)
                oos_records = [r for r in _raw_episode_records(frames[cell["symbol"]], config, "OOS", cell["symbol"], oos_bounds) if r["regime"] == cell["regime"] and r["horizon"] == cell["horizon"]]
                if not oos_records:
                    continue
                oos_metrics = _entry_metrics(pd.Series([r["forward_return"] for r in oos_records]), pd.Series([r["mfe"] for r in oos_records]), pd.Series([r["mae"] for r in oos_records]))
                if not _passes_entry_gate(oos_metrics):
                    continue
                best_survivor = {"dev": cell, "validation": val_metrics, "oos": oos_metrics, "oos_records": oos_records}
                break

            if best_survivor is None:
                registry["hypotheses"][config.key] = {"status": "REJECTED_VALIDATION_OR_OOS", "family": config.family, "dev_survivors": len(dev_survivors)}
                _save_registry(registry)
                _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_VALIDATION_OR_OOS")
                continue

            robust_ok, robust_reason = _robustness_check(best_survivor["oos_records"])
            if not robust_ok:
                registry["hypotheses"][config.key] = {"status": "REJECTED_ROBUSTNESS", "family": config.family, "reason": robust_reason}
                _save_registry(registry)
                _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_ROBUSTNESS REASON={robust_reason}")
                continue

            cost = _cost_stress(best_survivor["oos_records"])
            bootstrap = _bootstrap([r["forward_return"] for r in best_survivor["oos_records"]])
            if cost["net_pf_stress"] <= 1.0 or cost["net_expectancy_stress"] <= 0.0:
                registry["hypotheses"][config.key] = {"status": "REJECTED_COSTS", "family": config.family, "cost_stress": cost}
                _save_registry(registry)
                _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=REJECTED_COSTS NET_PF_STRESS={cost['net_pf_stress']:.2f}")
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
            _log(f"PIPELINE_STAGE: DISCOVERY CURRENT_HYPOTHESIS={config.family} CURRENT_STAGE=CANDIDATE_FOUND CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES}")

        final_status = "TARGET_REACHED_AWAITING_FINAL_HOLDOUT" if len(candidates) >= TARGET_CANDIDATES else "SEARCH_SPACE_EXHAUSTED"
        registry["status"] = final_status
        _save_registry(registry)
        _log(f"PIPELINE_STAGE: DISCOVERY {final_status} CANDIDATES_FOUND={len(candidates)}/{TARGET_CANDIDATES} HYPOTHESES_TESTED={registry['totals']['hypotheses_tested']} TOTAL_CONTEXT_CELLS={registry['totals']['cells_evaluated']}")
    finally:
        _release_lock()
