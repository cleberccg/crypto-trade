"""Microstructure entry-edge Discovery on Binance Spot aggTrades (2025-01, BTC/ETH).

Genuinely new information vs OHLCV/extended klines: uses per-trade aggressor
side (maker/taker), trade arrival sequence and trade size distribution to
build features that cannot be reconstructed from bar aggregates alone
(order-flow persistence, bursts, block-trade concentration).

Reuses the existing entry-edge-first scientific protocol from
strategy_discovery_cycle1.py: same gate constants, same statistical metrics
(_entry_metrics), same market regime classifier (classify_market_regimes),
same episode-dedup principle (a persistent signal counts once). Only the bar
construction (aggTrades -> 1-minute bars with microstructure features) and
the hypothesis definitions are new, because OHLCV cannot express them.

Does not touch BacktestEngine, RiskManager, PositionSizer or Paper Live.
Read/derive only; no strategy is registered or deployed from this script.
"""
from __future__ import annotations

import gzip
import json
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
    ENTRY_MIN_EFFECT_BPS,
    ENTRY_MIN_EPISODES,
    ENTRY_MIN_T_STAT,
    _entry_metrics,
    _passes_entry_gate,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "binance_spot_aggtrades_btc_eth_2025-01.jsonl.gz"
BAR_CACHE_DIR = BASE_DIR / "data" / "cache_minute_bars"
OUT_JSON = BASE_DIR / "discovery_microstructure_aggtrades_latest.json"
OUT_MD = BASE_DIR / "discovery_microstructure_aggtrades_latest.md"

# Independently verified via validate_aggtrades.py (2 reproducible runs,
# streamed gzip with per-member CRC check). Cache is only trusted if its
# recorded hash matches this value.
DATASET_HASH = "92d692b65e17571e9248af3a51e9573f53884a39840bfc74996da73713ec73db"

SYMBOLS = ("BTC/USDT", "ETH/USDT")
BAR_FREQ = "1min"
# Jan 2025 is a single ~31-day window; split by wall-clock time, not by
# resampling/shuffling, to avoid leaking future information into DEV.
DEV_END = pd.Timestamp("2025-01-19T00:00:00Z")        # ~60% (18/31 days)
VALIDATION_END = pd.Timestamp("2025-01-26T00:00:00Z")  # ~23% (7/31 days)
# OOS: 2025-01-26 -> 2025-02-01 (~19%, 6/31 days)

MICROSTRUCTURE_MIN_EFFECT_BPS = ENTRY_MIN_EFFECT_BPS  # reuse the same gate, no relaxation


def _log(message: str) -> None:
    print(message, flush=True)


def build_minute_bars(symbol: str) -> pd.DataFrame:
    """Stream aggTrades for one symbol and build 1-minute microstructure bars.

    Binance aggTrades field `m` = isBuyerMaker. m=True -> buyer is maker ->
    seller is the aggressor (sell-side pressure). m=False -> buyer is the
    aggressor (buy-side pressure).
    """
    rows: list[dict[str, Any]] = []
    bucket_ts: int | None = None
    o = h = l = c = 0.0
    buy_vol = sell_vol = 0.0
    trade_count = 0
    max_run = 0
    run_side: bool | None = None
    run_len = 0
    processed = 0
    started = time.monotonic()

    def flush_bucket() -> None:
        nonlocal max_run
        if bucket_ts is None or trade_count == 0:
            return
        total_vol = buy_vol + sell_vol
        rows.append(
            {
                "timestamp": pd.Timestamp(bucket_ts, unit="ms", tz="UTC"),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": total_vol,
                "buy_volume": buy_vol,
                "sell_volume": sell_vol,
                "trade_count": trade_count,
                "avg_trade_size": total_vol / trade_count if trade_count else 0.0,
                "max_aggressor_run": max_run,
            }
        )

    with gzip.open(DATA_PATH, "rb") as handle:
        for raw_line in handle:
            processed += 1
            if processed % 10_000_000 == 0:
                elapsed = time.monotonic() - started
                _log(f"STATUS: RUNNING CURRENT_STAGE=BAR_BUILD CURRENT_SYMBOL={symbol} PROCESSED={processed} ELAPSED={elapsed:.0f}s")
            row = json.loads(raw_line)
            if row.get("symbol") != symbol:
                continue
            ts = int(row["T"])
            price = float(row["p"])
            qty = float(row["q"])
            is_buyer_maker = bool(row["m"])
            is_buy_aggressor = not is_buyer_maker

            minute_ts = (ts // 60_000) * 60_000
            if minute_ts != bucket_ts:
                flush_bucket()
                bucket_ts = minute_ts
                o = h = l = c = price
                buy_vol = sell_vol = 0.0
                trade_count = 0
                max_run = 0

            h = max(h, price)
            l = min(l, price)
            c = price
            if is_buy_aggressor:
                buy_vol += qty
            else:
                sell_vol += qty
            trade_count += 1

            if run_side == is_buy_aggressor:
                run_len += 1
            else:
                run_side = is_buy_aggressor
                run_len = 1
            max_run = max(max_run, run_len)

    flush_bucket()
    frame = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return frame


def add_microstructure_features(frame: pd.DataFrame, window: int = 30) -> pd.DataFrame:
    df = frame.copy()
    total_vol = df["volume"].replace(0, np.nan)
    df["signed_volume"] = df["buy_volume"] - df["sell_volume"]
    df["imbalance_ratio"] = (df["signed_volume"] / total_vol).fillna(0.0)
    df["cvd"] = df["signed_volume"].cumsum()

    # Block-flow: imbalance amplified only when average trade size this bar is
    # unusually large vs its own trailing history (causal, no lookahead).
    rolling_avg_size = df["avg_trade_size"].rolling(window, min_periods=window).mean().shift(1)
    df["size_ratio"] = (df["avg_trade_size"] / rolling_avg_size).replace([np.inf, -np.inf], np.nan)
    df["block_flow_signal"] = df["imbalance_ratio"] * df["size_ratio"]

    # Burst intensity: trades/min z-scored against trailing history, combined
    # with the max same-side aggressor run observed inside the bar.
    rolling_mean_tc = df["trade_count"].rolling(window, min_periods=window).mean().shift(1)
    rolling_std_tc = df["trade_count"].rolling(window, min_periods=window).std().shift(1)
    df["intensity_z"] = ((df["trade_count"] - rolling_mean_tc) / rolling_std_tc).replace([np.inf, -np.inf], np.nan)
    df["burst_persistence_signal"] = df["intensity_z"] * np.sign(df["signed_volume"]) * (df["max_aggressor_run"] / df["trade_count"].replace(0, np.nan))

    # CVD acceleration: delta_t (per-bar signed order flow) -> velocity (bar-
    # over-bar change in delta_t) -> acceleration (bar-over-bar change in
    # velocity), z-scored against trailing history. This targets a change in
    # the *dynamics* of aggressor flow, not its level (already rejected as
    # TAKER_FLOW_IMBALANCE) nor a smoothed slope.
    delta_t = df["signed_volume"]
    velocity = delta_t.diff(1)
    acceleration_raw = velocity.diff(1)
    rolling_mean_accel = acceleration_raw.rolling(window, min_periods=window).mean().shift(1)
    rolling_std_accel = acceleration_raw.rolling(window, min_periods=window).std().shift(1)
    df["flow_velocity"] = velocity
    df["cvd_acceleration"] = ((acceleration_raw - rolling_mean_accel) / rolling_std_accel).replace([np.inf, -np.inf], np.nan)

    # Flow absorption: heavy one-sided aggression (top of its own trailing
    # distribution) that fails to move price proportionally (price move is
    # BELOW its own trailing average). Signed by aggressor direction so
    # "long_above" = large buy-side absorption, "long_below" = large
    # sell-side absorption. Tests whether absorption precedes reversal or
    # continuation -- OHLCV alone cannot separate "big volume, flat price"
    # from "big volume, big price move" at this resolution.
    flow_magnitude = df["signed_volume"].abs()
    rolling_mean_flow = flow_magnitude.rolling(window, min_periods=window).mean().shift(1)
    rolling_std_flow = flow_magnitude.rolling(window, min_periods=window).std().shift(1)
    flow_z = ((flow_magnitude - rolling_mean_flow) / rolling_std_flow).replace([np.inf, -np.inf], np.nan)

    price_move = (df["close"] / df["open"] - 1.0).abs()
    rolling_mean_move = price_move.rolling(window, min_periods=window).mean().shift(1)
    rolling_std_move = price_move.rolling(window, min_periods=window).std().shift(1)
    price_move_z = ((price_move - rolling_mean_move) / rolling_std_move).replace([np.inf, -np.inf], np.nan)

    absorption_mask = price_move_z < 0.0  # price moved less than its own trailing average
    df["flow_absorption_signal"] = (flow_z * np.sign(df["signed_volume"])).where(absorption_mask)
    return df


@dataclass(frozen=True)
class HypothesisConfig:
    name: str
    feature: str
    threshold: float
    direction: str  # "long_above" or "long_below"


HYPOTHESES = [
    # FLOW_ABSORPTION: 1.5/2.0 sigma produced 0 cells with >=100 episodes
    # (max 64-96 per cell) -- a sample-size problem, not an outcome-driven
    # choice. Recalibrated to 1.0 sigma (already characterized: ~1517-1645
    # total DEV episodes, ~84-91/day) per the same non-outcome-driven
    # frequency-first rule used for BURST_PERSISTENCE. BURST_PERSISTENCE and
    # CVD_ACCELERATION were already tested with adequate samples and
    # rejected (see discovery_microstructure_aggtrades_latest.json history).
    HypothesisConfig("FLOW_ABSORPTION", "flow_absorption_signal", 1.0, "long_above"),
    HypothesisConfig("FLOW_ABSORPTION", "flow_absorption_signal", -1.0, "long_below"),
    HypothesisConfig("FLOW_ABSORPTION", "flow_absorption_signal", 1.5, "long_above"),
    HypothesisConfig("FLOW_ABSORPTION", "flow_absorption_signal", -1.5, "long_below"),
]


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


def _entry_rows_for_config(
    frame: pd.DataFrame,
    config: HypothesisConfig,
    split: str,
    symbol: str,
) -> list[dict[str, Any]]:
    split_start, split_end = _split_bounds(frame, split)
    eligible = (frame.index >= split_start) & (frame.index < split_end)
    if eligible.sum() <= 100:
        return []

    feature = frame[config.feature]
    if config.direction == "long_above":
        entry = feature > config.threshold
    else:
        entry = feature < config.threshold
    entry = entry.fillna(False)
    starts = entry & ~entry.shift(fill_value=False)

    regimes = classify_market_regimes(frame.loc[frame.index < split_end, ["open", "high", "low", "close", "volume"]])

    records: list[dict[str, Any]] = []
    n = len(frame)
    warmup = 60
    positions = np.flatnonzero(starts.to_numpy(bool))
    for signal_position in positions:
        if signal_position < warmup or not bool(eligible[signal_position]):
            continue
        entry_position = signal_position + 1
        if entry_position >= n or not bool(eligible[entry_position]):
            continue
        entry_price = float(frame.iloc[entry_position]["open"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        direction_sign = 1.0 if config.direction == "long_above" else -1.0
        regime_key = str(regimes.loc[frame.index[signal_position], "regime_key"]) if frame.index[signal_position] in regimes.index else "unknown"
        for horizon in ENTRY_FORWARD_HORIZONS:
            exit_position = entry_position + horizon - 1
            if exit_position >= n or not bool(eligible[exit_position]):
                continue
            path = frame.iloc[entry_position : exit_position + 1]
            forward_return = direction_sign * (float(path.iloc[-1]["close"]) / entry_price - 1.0)
            mfe = direction_sign * (float(path["high"].max()) / entry_price - 1.0) if direction_sign > 0 else direction_sign * (float(path["low"].min()) / entry_price - 1.0)
            mae = direction_sign * (float(path["low"].min()) / entry_price - 1.0) if direction_sign > 0 else direction_sign * (float(path["high"].max()) / entry_price - 1.0)
            records.append(
                {
                    "hypothesis": config.name,
                    "feature": config.feature,
                    "threshold": config.threshold,
                    "direction": config.direction,
                    "symbol": symbol,
                    "regime": regime_key,
                    "horizon": horizon,
                    "forward_return": forward_return,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
    return records


def audit_config(frames: dict[str, pd.DataFrame], config: HypothesisConfig, split: str) -> list[dict[str, Any]]:
    all_records: list[dict[str, Any]] = []
    for symbol, frame in frames.items():
        all_records.extend(_entry_rows_for_config(frame, config, split, symbol))
    if not all_records:
        return []
    df = pd.DataFrame(all_records)
    keys = ["hypothesis", "feature", "threshold", "direction", "symbol", "regime", "horizon"]
    results = []
    for key, group in df.groupby(keys, sort=True):
        metrics = _entry_metrics(group["forward_return"], group["mfe"], group["mae"])
        results.append({**dict(zip(keys, key)), **metrics})
    return results


def _cache_path(symbol: str) -> Path:
    return BAR_CACHE_DIR / f"{symbol.replace('/', '_')}_1min_bars.parquet"


def _cache_meta_path(symbol: str) -> Path:
    return BAR_CACHE_DIR / f"{symbol.replace('/', '_')}_1min_bars.meta.json"


def load_or_build_minute_bars(symbol: str) -> pd.DataFrame:
    cache_path = _cache_path(symbol)
    meta_path = _cache_meta_path(symbol)
    if cache_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("dataset_hash") == DATASET_HASH and meta.get("source_file") == DATA_PATH.name:
            _log(f"STATUS: RUNNING CURRENT_STAGE=BARS_CACHE_HIT CURRENT_SYMBOL={symbol} CACHE_DATASET_HASH={meta.get('dataset_hash')}")
            return pd.read_parquet(cache_path)
        _log(f"STATUS: RUNNING CURRENT_STAGE=BARS_CACHE_STALE CURRENT_SYMBOL={symbol} (hash mismatch, rebuilding)")
    bars = build_minute_bars(symbol)
    BAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(cache_path)
    meta_path.write_text(
        json.dumps({"dataset_hash": DATASET_HASH, "source_file": DATA_PATH.name, "symbol": symbol, "bars": len(bars)}, indent=2),
        encoding="utf-8",
    )
    return bars


def characterize_signal_frequency(
    frames: dict[str, pd.DataFrame],
    feature: str,
    thresholds: list[float],
    direction: str,
) -> list[dict[str, Any]]:
    """Descriptive-only characterization (no returns/PF) of how often a raw
    signal fires on the DEV split, used to pick ONE frozen threshold before
    any outcome is examined (avoids choosing a threshold by its PF)."""
    rows: list[dict[str, Any]] = []
    for threshold in thresholds:
        total_signals = 0
        total_episodes = 0
        durations: list[int] = []
        per_symbol_episodes: dict[str, int] = {}
        for symbol, frame in frames.items():
            dev_start, dev_end = _split_bounds(frame, "DEV")
            eligible = (frame.index >= dev_start) & (frame.index < dev_end)
            sub = frame.loc[eligible]
            feat = sub[feature]
            entry = (feat > threshold) if direction == "long_above" else (feat < threshold)
            entry = entry.fillna(False)
            total_signals += int(entry.sum())
            starts = entry & ~entry.shift(fill_value=False)
            episode_id = starts.cumsum().where(entry)
            episode_durations = entry.groupby(episode_id).sum()
            durations.extend(int(d) for d in episode_durations.tolist())
            per_symbol_episodes[symbol] = int(starts.sum())
            total_episodes += int(starts.sum())
        days = (DEV_END - pd.Timestamp("2025-01-01T00:00:00Z")).total_seconds() / 86400.0
        rows.append(
            {
                "feature": feature,
                "direction": direction,
                "threshold": threshold,
                "total_signals": total_signals,
                "independent_episodes": total_episodes,
                "episodes_per_day": round(total_episodes / days, 2) if days else 0.0,
                "avg_duration_bars": round(float(np.mean(durations)), 2) if durations else 0.0,
                "episodes_by_symbol": per_symbol_episodes,
            }
        )
    return rows


def main() -> int:
    started = time.monotonic()
    _log("STATUS: RUNNING CURRENT_STAGE=LOAD_AGGTRADES")
    frames: dict[str, pd.DataFrame] = {}
    for symbol in SYMBOLS:
        bars = load_or_build_minute_bars(symbol)
        frames[symbol] = add_microstructure_features(bars)
        _log(f"STATUS: RUNNING CURRENT_STAGE=BARS_BUILT CURRENT_SYMBOL={symbol} BARS={len(bars)}")

    frequency_report: dict[str, list[dict[str, Any]]] = {}
    for feature, direction, thresholds in (
        ("flow_absorption_signal", "long_above", [1.0, 1.5, 2.0]),
        ("flow_absorption_signal", "long_below", [-1.0, -1.5, -2.0]),
    ):
        rows = characterize_signal_frequency(frames, feature, thresholds, direction)
        frequency_report[f"{feature}|{direction}"] = rows
        for row in rows:
            _log(
                f"STATUS: RUNNING CURRENT_STAGE=FREQUENCY_CHARACTERIZATION FEATURE={feature} DIRECTION={direction} "
                f"THRESHOLD={row['threshold']} TOTAL_SIGNALS={row['total_signals']} EPISODES={row['independent_episodes']} "
                f"EPISODES_PER_DAY={row['episodes_per_day']} AVG_DURATION_BARS={row['avg_duration_bars']} "
                f"BY_SYMBOL={row['episodes_by_symbol']}"
            )

    dev_results: dict[str, list[dict[str, Any]]] = {}
    for config in HYPOTHESES:
        key = f"{config.name}|{config.feature}|{config.threshold}|{config.direction}"
        _log(f"STATUS: RUNNING CURRENT_STAGE=DEV_AUDIT CURRENT_HYPOTHESIS={key}")
        dev_results[key] = audit_config(frames, config, "DEV")

    dev_survivors = {key: [row for row in rows if _passes_entry_gate(row)] for key, rows in dev_results.items()}
    dev_survivor_count = sum(len(rows) for rows in dev_survivors.values())
    for key, rows in dev_results.items():
        if not rows:
            _log(f"STATUS: RUNNING CURRENT_STAGE=DEV_DIAGNOSTIC CURRENT_HYPOTHESIS={key} NO_EPISODES_FOUND")
            continue
        best = max(rows, key=lambda row: row["episodes"])
        _log(
            f"STATUS: RUNNING CURRENT_STAGE=DEV_DIAGNOSTIC CURRENT_HYPOTHESIS={key} "
            f"CELLS={len(rows)} BEST_EPISODES={best['episodes']} BEST_PF={best['gross_pf']:.2f} "
            f"BEST_EFFECT_BPS={best['effect_bps']:.2f} BEST_T_STAT={best['t_stat']:.2f} "
            f"MAX_EPISODES_ANY_CELL={max(r['episodes'] for r in rows)} "
            f"CELLS_WITH_100PLUS_EPISODES={sum(1 for r in rows if r['episodes'] >= ENTRY_MIN_EPISODES)}"
        )
    _log(f"STATUS: RUNNING CURRENT_STAGE=DEV_DONE DEV_SURVIVORS={dev_survivor_count}")

    validation_results: dict[str, list[dict[str, Any]]] = {}
    validation_survivors: dict[str, list[dict[str, Any]]] = {}
    for config in HYPOTHESES:
        key = f"{config.name}|{config.feature}|{config.threshold}|{config.direction}"
        if not dev_survivors.get(key):
            continue
        _log(f"STATUS: RUNNING CURRENT_STAGE=VALIDATION_AUDIT CURRENT_HYPOTHESIS={key}")
        rows = audit_config(frames, config, "VALIDATION")
        validation_results[key] = rows
        survivors = []
        for dev_row in dev_survivors[key]:
            match = next((r for r in rows if all(r[k] == dev_row[k] for k in ("symbol", "regime", "horizon"))), None)
            if match and _passes_entry_gate(match):
                survivors.append(match)
        validation_survivors[key] = survivors

    validation_survivor_count = sum(len(rows) for rows in validation_survivors.values())
    _log(f"STATUS: RUNNING CURRENT_STAGE=VALIDATION_DONE VALIDATION_SURVIVORS={validation_survivor_count}")

    oos_results: dict[str, list[dict[str, Any]]] = {}
    oos_survivors: dict[str, list[dict[str, Any]]] = {}
    for config in HYPOTHESES:
        key = f"{config.name}|{config.feature}|{config.threshold}|{config.direction}"
        if not validation_survivors.get(key):
            continue
        _log(f"STATUS: RUNNING CURRENT_STAGE=OOS_AUDIT CURRENT_HYPOTHESIS={key}")
        rows = audit_config(frames, config, "OOS")
        oos_results[key] = rows
        survivors = []
        for val_row in validation_survivors[key]:
            match = next((r for r in rows if all(r[k] == val_row[k] for k in ("symbol", "regime", "horizon"))), None)
            if match and _passes_entry_gate(match):
                survivors.append(match)
        oos_survivors[key] = survivors

    oos_survivor_count = sum(len(rows) for rows in oos_survivors.values())
    elapsed = time.monotonic() - started
    _log(f"STATUS: COMPLETED CURRENT_STAGE=OOS_DONE OOS_SURVIVORS={oos_survivor_count} ELAPSED={elapsed:.0f}s")

    summary = {
        "dataset_hash": DATASET_HASH,
        "hypotheses_tested": sorted({c.name for c in HYPOTHESES}),
        "configurations_tested": len(HYPOTHESES),
        "dev_survivors": dev_survivor_count,
        "validation_survivors": validation_survivor_count,
        "oos_survivors": oos_survivor_count,
        "oos_survivor_rows": [row for rows in oos_survivors.values() for row in rows],
        "dev_results_raw": dev_results,
        "frequency_characterization": frequency_report,
        "gate": {
            "min_episodes": ENTRY_MIN_EPISODES,
            "min_effect_bps": ENTRY_MIN_EFFECT_BPS,
            "min_t_stat": ENTRY_MIN_T_STAT,
        },
        "splits": {
            "dev_end": DEV_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
        },
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    _log(f"RESULTS_WRITTEN: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
