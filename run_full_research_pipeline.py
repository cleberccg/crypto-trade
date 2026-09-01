"""Single continuous research pipeline: COLLECTION -> VALIDATION -> FEATURE
CACHE -> AUTONOMOUS DISCOVERY, chained automatically in one process.

Reuses (does not duplicate):
- collect_aggtrades_bulk.py (partitioned Binance bulk collector, lock/manifest)
- discover_microstructure_aggtrades.py's bar-construction/feature conventions
- strategy_discovery_cycle1.py gate constants and statistics
- run_autonomous_strategy_discovery.py's hypothesis funnel (imported and
  pointed at the new dataset via module-level monkeypatch of its data
  loading, not reimplemented)

If a stage fails, only that stage's pipeline stops; the failure and its
exact cause are logged, no speculative fixes are attempted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import collect_aggtrades_bulk as bulk

BASE_DIR = Path(__file__).resolve().parent
PIPELINE_LOCK = BASE_DIR / "research_pipeline.lock"
PIPELINE_STATE_PATH = BASE_DIR / "research_pipeline_state.json"
CACHE_ROOT = BASE_DIR / "data" / "cache_minute_bars_v2"
TIMEFRAMES = ("1m", "5m", "15m")
FEATURE_VERSION = "v2"

# Temporal protection: FINAL_HOLDOUT is never touched by Discovery.
DEV_END = pd.Timestamp("2025-09-01T00:00:00Z")
VALIDATION_END = pd.Timestamp("2026-02-01T00:00:00Z")
OOS_END = pd.Timestamp("2026-06-01T00:00:00Z")
# [OOS_END, FINAL_HOLDOUT_END) is FINAL_HOLDOUT -- locked, not readable by Discovery.


def _log(message: str) -> None:
    print(message, flush=True)


def _acquire_lock() -> None:
    try:
        fd = os.open(str(PIPELINE_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = PIPELINE_LOCK.read_text(encoding="utf-8").strip() if PIPELINE_LOCK.exists() else "?"
        raise RuntimeError(f"Lock {PIPELINE_LOCK} exists (pid={existing}). Another pipeline instance may be running.")
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)


def _release_lock() -> None:
    PIPELINE_LOCK.unlink(missing_ok=True)


def _set_stage(stage: str, **extra: Any) -> None:
    state = {"stage": stage, "updated_at": datetime.now(timezone.utc).isoformat(), **extra}
    PIPELINE_STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _known_gap_keys(manifest: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for gap in manifest.get("known_gaps", []):
        symbol = gap.get("symbol")
        period = gap.get("period")
        if symbol and period:
            keys.add(f"{symbol}|{period}")
    return keys


def _free_disk_gb(path: Path) -> float:
    usage = shutil.disk_usage(path.anchor)
    return usage.free / 1e9


def stage_collection() -> bool:
    """Waits for an already-running bulk collector, or runs it in-process."""
    _log("PIPELINE_STAGE: COLLECTION")
    while bulk.LOCK_PATH.exists():
        _log("PIPELINE_STAGE: COLLECTION STATUS=WAITING_FOR_RUNNING_COLLECTOR")
        time.sleep(30)
    manifest = bulk._load_manifest()
    known_gap_keys = _known_gap_keys(manifest)
    partitions = bulk._discover_partitions()
    pending = [
        p
        for p in partitions
        if p.key not in known_gap_keys
        and manifest["partitions"].get(p.key, {}).get("VALIDATED") not in ("YES", "NOT_AVAILABLE")
    ]
    if pending:
        _log(f"PIPELINE_STAGE: COLLECTION REMAINING_PARTITIONS={len(pending)}")
        exit_code = bulk.main()
        if exit_code != 0:
            _log("PIPELINE_STAGE: COLLECTION FAILED -- stopping pipeline (research/pipeline stage only)")
            return False
    _log("PIPELINE_STAGE: COLLECTION COMPLETED")
    return True


def stage_global_validation() -> dict[str, Any] | None:
    _log("PIPELINE_STAGE: VALIDATION")
    manifest = bulk._load_manifest()
    known_gap_keys = _known_gap_keys(manifest)
    valid_partitions = {k: v for k, v in manifest["partitions"].items() if v.get("VALIDATED") == "YES"}
    invalid_partitions = {
        k: v
        for k, v in manifest["partitions"].items()
        if k not in known_gap_keys and v.get("VALIDATED") not in ("YES", "NOT_AVAILABLE")
    }
    if invalid_partitions:
        _log(f"PIPELINE_STAGE: VALIDATION FAILED invalid_partitions={list(invalid_partitions)}")
        return None

    per_symbol_records: dict[str, int] = {}
    first_ts: dict[str, str] = {}
    last_ts: dict[str, str] = {}
    for entry in valid_partitions.values():
        symbol = entry["SYMBOL"]
        per_symbol_records[symbol] = per_symbol_records.get(symbol, 0) + entry["RECORDS"]
        if symbol not in first_ts or entry["FIRST_TIMESTAMP"] < first_ts[symbol]:
            first_ts[symbol] = entry["FIRST_TIMESTAMP"]
        if symbol not in last_ts or entry["LAST_TIMESTAMP"] > last_ts[symbol]:
            last_ts[symbol] = entry["LAST_TIMESTAMP"]

    manifest_hash_input = json.dumps(
        sorted((k, v["HASH"]) for k, v in valid_partitions.items()), sort_keys=True
    ).encode("utf-8")
    dataset_manifest_hash = hashlib.sha256(manifest_hash_input).hexdigest()

    summary = {
        "DATASET_VALID": "YES",
        "SYMBOLS": sorted(per_symbol_records),
        "PARTITIONS": len(manifest["partitions"]),
        "VALID_PARTITIONS": len(valid_partitions),
        "INVALID_PARTITIONS": len(invalid_partitions),
        "TOTAL_RECORDS": sum(per_symbol_records.values()),
        "RECORDS_BY_SYMBOL": per_symbol_records,
        "FIRST_TIMESTAMP_BY_SYMBOL": first_ts,
        "LAST_TIMESTAMP_BY_SYMBOL": last_ts,
        "DATASET_MANIFEST_HASH": dataset_manifest_hash,
    }
    _log(f"PIPELINE_STAGE: VALIDATION COMPLETED DATASET_MANIFEST_HASH={dataset_manifest_hash} TOTAL_RECORDS={summary['TOTAL_RECORDS']}")
    return summary


def _build_1m_bars(symbol: str) -> pd.DataFrame:
    """Streams every validated partition parquet for one symbol (already
    columnar/compressed; no need to touch the raw Binance zips again) and
    aggregates into 1-minute microstructure bars."""
    manifest = bulk._load_manifest()
    parts = sorted(
        (v for v in manifest["partitions"].values() if v.get("SYMBOL") == symbol and v.get("VALIDATED") == "YES"),
        key=lambda v: v["PERIOD"],
    )
    frames = []
    for entry in parts:
        period = entry["PERIOD"]
        granularity = "monthly" if len(period) == 7 else "daily"
        path = bulk.Partition(symbol, period, granularity).output_path
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["p", "q", "T", "m"])
        minute = (df["T"] // 60_000) * 60_000
        is_buy = ~df["m"]
        signed_qty = np.where(is_buy, df["q"], -df["q"])
        bucket = pd.DataFrame({
            "minute": minute, "price": df["p"], "qty": df["q"], "signed_qty": signed_qty,
        })
        grouped = bucket.groupby("minute")
        bar = pd.DataFrame({
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "volume": grouped["qty"].sum(),
            "signed_volume": grouped["signed_qty"].sum(),
            "trade_count": grouped["price"].size(),
        })
        bar["buy_volume"] = (bar["volume"] + bar["signed_volume"]) / 2.0
        bar["sell_volume"] = bar["volume"] - bar["buy_volume"]
        bar["avg_trade_size"] = bar["volume"] / bar["trade_count"]
        bar.index = pd.to_datetime(bar.index, unit="ms", utc=True)
        frames.append(bar)
    full = pd.concat(frames).sort_index()
    full = full[~full.index.duplicated(keep="first")]
    return full


def _resample(bars_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe == "1m":
        return bars_1m
    rule = {"5m": "5min", "15m": "15min"}[timeframe]
    agg = bars_1m.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "signed_volume": "sum", "trade_count": "sum",
        "buy_volume": "sum", "sell_volume": "sum",
    }).dropna(subset=["open"])
    agg["avg_trade_size"] = agg["volume"] / agg["trade_count"].replace(0, np.nan)
    return agg


def stage_feature_cache(dataset_manifest_hash: str) -> Path:
    _log("PIPELINE_STAGE: FEATURE_CACHE")
    cache_dir = CACHE_ROOT / dataset_manifest_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    errors = 0
    tasks_total = len(bulk.SYMBOLS) * len(TIMEFRAMES)
    task_index = 0

    for symbol in bulk.SYMBOLS:
        bars_1m = _build_1m_bars(symbol)
        for timeframe in TIMEFRAMES:
            task_index += 1
            progress = task_index / tasks_total * 100.0
            try:
                bars = _resample(bars_1m, timeframe)
                out_path = cache_dir / f"{symbol}_{timeframe}.parquet"
                bars.to_parquet(out_path)
                meta_path = cache_dir / f"{symbol}_{timeframe}.meta.json"
                meta_path.write_text(json.dumps({
                    "SOURCE_DATASET_HASH": dataset_manifest_hash, "SYMBOL": symbol, "TIMEFRAME": timeframe,
                    "START": bars.index.min().isoformat() if len(bars) else None,
                    "END": bars.index.max().isoformat() if len(bars) else None,
                    "FEATURE_VERSION": FEATURE_VERSION, "ROWS": len(bars),
                }, indent=2), encoding="utf-8")
                output_size_mb = out_path.stat().st_size / 1e6 if out_path.exists() else 0.0
                elapsed = time.monotonic() - started
                rate = task_index / max(elapsed, 1e-9)
                eta_s = (tasks_total - task_index) / max(rate, 1e-9)
                _log(
                    "STAGE: FEATURE_CACHE "
                    f"SYMBOL: {symbol} "
                    f"TIMEFRAME: {timeframe} "
                    f"PROGRESS: {progress:.1f}% ({task_index}/{tasks_total}) "
                    f"ROWS: {len(bars)} "
                    f"OUTPUT_SIZE: {output_size_mb:.2f}MB "
                    f"FREE_DISK: {_free_disk_gb(BASE_DIR):.1f}GB "
                    f"ELAPSED: {elapsed:.0f}s "
                    f"ETA: {eta_s:.0f}s "
                    f"ERRORS: {errors}"
                )
            except Exception:  # noqa: BLE001 - stage-level reporting, re-raise to fail fast
                errors += 1
                elapsed = time.monotonic() - started
                _log(
                    "STAGE: FEATURE_CACHE "
                    f"SYMBOL: {symbol} "
                    f"TIMEFRAME: {timeframe} "
                    f"PROGRESS: {progress:.1f}% ({task_index}/{tasks_total}) "
                    "ROWS: 0 "
                    "OUTPUT_SIZE: 0.00MB "
                    f"FREE_DISK: {_free_disk_gb(BASE_DIR):.1f}GB "
                    f"ELAPSED: {elapsed:.0f}s "
                    "ETA: ? "
                    f"ERRORS: {errors}"
                )
                raise
    _log(f"PIPELINE_STAGE: FEATURE_CACHE COMPLETED CACHE_DIR={cache_dir}")
    return cache_dir


def stage_discovery(cache_dir: Path, dataset_manifest_hash: str) -> None:
    _log("PIPELINE_STAGE: DISCOVERY")
    import run_autonomous_strategy_research_v3 as discovery_v3
    # v3 self-loads the validated cache/hash context and remains restart-safe.
    discovery_v3.run()


def main() -> int:
    _acquire_lock()
    try:
        if not stage_collection():
            _set_stage("COLLECTION_FAILED")
            return 1
        validation = stage_global_validation()
        if validation is None:
            _set_stage("VALIDATION_FAILED")
            return 1
        _set_stage("VALIDATION_COMPLETED", **validation)

        cache_dir = stage_feature_cache(validation["DATASET_MANIFEST_HASH"])
        _set_stage("FEATURE_CACHE_COMPLETED", cache_dir=str(cache_dir))

        stage_discovery(cache_dir, validation["DATASET_MANIFEST_HASH"])
        _set_stage("DISCOVERY_LAUNCHED")
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
