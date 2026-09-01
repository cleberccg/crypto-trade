"""Bulk order-book/depth collector using Binance's official historical data
archive (data.binance.vision).

SOURCE AUDIT (see COPILOT_INSTRUCTIONS / session report for full detail):
- Binance SPOT has NO historical order-book/depth/bookTicker archive at all
  (data/spot/daily only has aggTrades, klines, trades -- confirmed by listing
  the official S3 bucket). Only live snapshots via fetch_order_book exist
  (exchange/binance_client.py, exchange/binance_market_data_client.py), which
  are NOT historical and NOT bulk.
- Binance USDⓈ-M FUTURES (data/futures/um/daily/) DOES publish two archives:
    * bookDepth: aggregated depth at 12 fixed percentage-from-mid bands
      (-5,-4,-3,-2,-1,-0.2,0.2,1,2,3,4,5), snapshotted every ~30s. Available
      continuously 2023-01-01 -> present (confirmed). ~0.4-0.6 MB/day/symbol
      compressed -- small.
    * bookTicker: full best-bid/ask update stream. Confirmed to exist only
      as a handful of monthly files around 2023-05 (~2GB compressed for a
      SINGLE symbol-month) and is NOT continuously published as daily files
      afterwards (empty listings for 2025-06, 2025-12, 2026-08). Not usable
      for a continuous multi-year history: this script does NOT collect it.
- This is FUTURES (perpetual) market data, not SPOT. It is the closest
  official, continuously-available historical order-book proxy. BID_ASK_
  SPREAD / MICROPRICE (which need true L1 best bid/ask) are NOT derivable
  from bookDepth; only depth/imbalance/liquidity features across percentage
  bands are derivable. This is documented, not invented.

Storage: partitioned, columnar Parquet (one file per symbol/day):
    data/orderbook_depth/{SYMBOL}/{YEAR}/{MONTH}/{SYMBOL}_{YEAR}_{MONTH}_{DAY}.parquet

Restart-safe: manifest (data/orderbook_depth/manifest.json) records one entry
per partition with VALIDATED=YES only after integrity checks pass; completed
partitions are never re-downloaded. Protected by a process lock (same
pattern as collect_aggtrades_bulk.py).

Disk-safety gate: aborts BEFORE starting a new partition if free disk space
drops below MIN_FREE_DISK_GB. Each partition is downloaded, parsed, written
to Parquet, validated, and the temporary zip deleted immediately.

Does not touch MySQL / the operational database. Does not touch Paper Live.
Does not touch BacktestEngine, RiskManager, PositionSizer or ClassicDonchianBreakout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from utils.helpers import retry

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = BASE_DIR / "data" / "orderbook_depth"
MANIFEST_PATH = OUT_ROOT / "manifest.json"
LOCK_PATH = BASE_DIR / "data" / "orderbook_depth_bulk.lock"
TMP_DIR = OUT_ROOT / "_tmp"

SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEFAULT_DATE_START = date(2024, 1, 1)
MIN_FREE_DISK_GB = 15.0  # same safety margin convention as collect_aggtrades_bulk.py
BULK_BASE_URL = "https://data.binance.vision/data/futures/um/daily/bookDepth"
EXPECTED_PERCENTAGES = (-5.0, -4.0, -3.0, -2.0, -1.0, -0.2, 0.2, 1.0, 2.0, 3.0, 4.0, 5.0)


def _log(message: str) -> None:
    print(message, flush=True)


def _acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = LOCK_PATH.read_text(encoding="utf-8").strip() if LOCK_PATH.exists() else "?"
        raise RuntimeError(f"Lock {LOCK_PATH} exists (pid={existing}). Another orderbook depth collector instance may be running.")
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _free_disk_gb(path: Path) -> float:
    usage = shutil.disk_usage(path.anchor)
    return usage.free / 1e9


def _load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"partitions": {}}


def _save_manifest(manifest: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)  # atomic write


@dataclass(frozen=True)
class Partition:
    symbol: str
    period: str  # "2026-08-15" (daily only -- no monthly bookDepth archive exists)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.period}"

    @property
    def url(self) -> str:
        return f"{BULK_BASE_URL}/{self.symbol}/{self.symbol}-bookDepth-{self.period}.zip"

    @property
    def output_path(self) -> Path:
        year, month, day = self.period.split("-")
        return OUT_ROOT / self.symbol / year / month / f"{self.symbol}_{year}_{month}_{day}.parquet"


@retry(max_attempts=3, delay_seconds=5.0)
def _head(url: str) -> requests.Response:
    return requests.head(url, timeout=30)


@retry(max_attempts=3, delay_seconds=5.0)
def _download(url: str, dest: Path) -> int:
    with requests.get(url, timeout=120, stream=True) as response:
        response.raise_for_status()
        size = 0
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                handle.write(chunk)
                size += len(chunk)
    return size


def _discover_partitions(start: date, end_inclusive: date) -> list[Partition]:
    partitions: list[Partition] = []
    for symbol in SYMBOLS:
        cursor = start
        while cursor <= end_inclusive:
            partitions.append(Partition(symbol, cursor.isoformat()))
            cursor += timedelta(days=1)
    return partitions


def _parse_bookdepth_zip(zip_path: Path) -> pd.DataFrame:
    import zipfile

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise RuntimeError(f"Unexpected archive contents in {zip_path}: {names}")
        with archive.open(names[0]) as csv_handle:
            df = pd.read_csv(csv_handle)
    expected_cols = {"timestamp", "percentage", "depth", "notional"}
    if set(df.columns) != expected_cols:
        raise RuntimeError(f"Unexpected columns in {zip_path}: {list(df.columns)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["percentage"] = pd.to_numeric(df["percentage"], errors="coerce")
    df["depth"] = pd.to_numeric(df["depth"], errors="coerce")
    df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
    return df.sort_values(["timestamp", "percentage"]).reset_index(drop=True)


def _validate_partition(df: pd.DataFrame, partition: Partition) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if df.empty:
        return False, ["empty_dataframe"]
    if df[["timestamp", "percentage", "depth", "notional"]].isna().any().any():
        issues.append("missing_values")
    if not df["timestamp"].is_monotonic_increasing:
        issues.append("timestamp_not_sorted")
    unexpected_pct = sorted(set(df["percentage"].round(2)) - set(EXPECTED_PERCENTAGES))
    if unexpected_pct:
        issues.append(f"unexpected_percentage_levels={unexpected_pct}")
    if (df["depth"] < 0).any():
        issues.append(f"negative_depth={int((df['depth'] < 0).sum())}")
    if (df["notional"] < 0).any():
        issues.append(f"negative_notional={int((df['notional'] < 0).sum())}")
    dup = df.duplicated(subset=["timestamp", "percentage"]).sum()
    if dup:
        issues.append(f"duplicate_timestamp_percentage={int(dup)}")
    # crossed-book style sanity check: at a given timestamp, larger |percentage|
    # away from mid must have >= notional accumulated on the same side than
    # closer percentages ONLY for cumulative archives; Binance's bookDepth is
    # per-band (not cumulative), so we instead check bands have plausible
    # (non-explosive) magnitude relative to the day's median.
    median_notional = df["notional"].median()
    if median_notional and (df["notional"] > median_notional * 1000).any():
        issues.append("notional_outlier_suspected")
    expected_day = datetime.strptime(partition.period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    first_ts = df["timestamp"].iloc[0].to_pydatetime()
    if abs((first_ts - expected_day).total_seconds()) > 26 * 3600:
        issues.append(f"first_timestamp_out_of_expected_period first_ts={first_ts.isoformat()}")
    return len(issues) == 0, issues


def _hash_dataframe(df: pd.DataFrame) -> str:
    sha256 = hashlib.sha256()
    sha256.update(pd.util.hash_pandas_object(df, index=False).values.tobytes())
    return sha256.hexdigest()


def process_partition(partition: Partition, manifest: dict[str, Any]) -> tuple[str, int, int]:
    """Returns (result, records, bytes_downloaded)."""
    existing = manifest["partitions"].get(partition.key)
    if existing and existing.get("VALIDATED") == "YES" and partition.output_path.exists():
        return "SKIPPED_ALREADY_VALIDATED", int(existing.get("RECORDS", 0)), 0

    head = _head(partition.url)
    if head.status_code == 404:
        manifest["partitions"][partition.key] = {"SYMBOL": partition.symbol, "PERIOD": partition.period, "VALIDATED": "NOT_AVAILABLE"}
        return "NOT_AVAILABLE", 0, 0
    head.raise_for_status()

    free_gb = _free_disk_gb(OUT_ROOT)
    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(f"DISK_SAFETY_ABORT free_disk_gb={free_gb:.1f} < MIN_FREE_DISK_GB={MIN_FREE_DISK_GB}")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TMP_DIR / f"{partition.symbol}_{partition.period}.zip"
    try:
        bytes_downloaded = _download(partition.url, zip_path)
        df = _parse_bookdepth_zip(zip_path)
        valid, issues = _validate_partition(df, partition)
        record_hash = _hash_dataframe(df)
        partition.output_path.parent.mkdir(parents=True, exist_ok=True)
        if valid:
            df.to_parquet(partition.output_path, compression="zstd", index=False)
        manifest["partitions"][partition.key] = {
            "SYMBOL": partition.symbol,
            "PERIOD": partition.period,
            "SOURCE": "BINANCE_BULK_FUTURES_UM_BOOKDEPTH",
            "RECORDS": int(len(df)),
            "FIRST_TIMESTAMP": df["timestamp"].iloc[0].isoformat(),
            "LAST_TIMESTAMP": df["timestamp"].iloc[-1].isoformat(),
            "OUTPUT_SIZE_BYTES": partition.output_path.stat().st_size if valid and partition.output_path.exists() else 0,
            "HASH": record_hash,
            "VALIDATED": "YES" if valid else "NO",
            "ISSUES": issues,
            "CREATED_AT": datetime.now(timezone.utc).isoformat(),
        }
        return ("VALIDATED" if valid else f"INVALID:{issues}"), int(len(df)), bytes_downloaded
    finally:
        zip_path.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Binance USDS-M Futures bookDepth (order-book proxy) history.")
    parser.add_argument("--start", default=DEFAULT_DATE_START.isoformat(), help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive (default: yesterday UTC)")
    parser.add_argument("--pilot-label", default=None, help="Free-text tag stored in manifest run log (e.g. PILOT)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else (datetime.now(timezone.utc).date() - timedelta(days=1))

    _acquire_lock()
    started = time.monotonic()
    total_bytes = 0
    total_records = 0
    try:
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        manifest = _load_manifest()
        partitions = _discover_partitions(start, end)
        total = len(partitions)
        completed = sum(1 for p in partitions if manifest["partitions"].get(p.key, {}).get("VALIDATED") in ("YES", "NOT_AVAILABLE"))
        run_tag = f" RUN_TAG={args.pilot_label}" if args.pilot_label else ""
        _log(f"STATUS: DOWNLOADING SOURCE=BINANCE_BULK_FUTURES_UM_BOOKDEPTH RANGE={start}..{end} TOTAL_PARTITIONS={total} PARTITIONS_COMPLETED={completed}{run_tag}")

        errors = 0
        for index, partition in enumerate(partitions, start=1):
            free_gb = _free_disk_gb(OUT_ROOT)
            try:
                result, records, bytes_downloaded = process_partition(partition, manifest)
            except Exception as exc:  # noqa: BLE001 - report and stop this pipeline only
                errors += 1
                _log(f"ERRORS: 1 PARTITION={partition.key} REASON={exc}")
                _save_manifest(manifest)
                if "DISK_SAFETY_ABORT" in str(exc):
                    _log("STATUS: ABORTED REASON=DISK_SAFETY_ABORT")
                    return 1
                continue
            _save_manifest(manifest)
            total_bytes += bytes_downloaded
            total_records += records
            output_size = sum(f.stat().st_size for f in OUT_ROOT.rglob("*.parquet"))
            elapsed = time.monotonic() - started
            rate = index / max(elapsed, 1e-9)
            eta_s = (total - index) / max(rate, 1e-9)
            _log(
                f"STATUS: DOWNLOADING SYMBOL={partition.symbol} PARTITION={partition.period} "
                f"PARTITIONS_COMPLETED={index}/{total} RECORDS={records} TOTAL_RECORDS={total_records} "
                f"BYTES_DOWNLOADED={total_bytes} OUTPUT_SIZE={output_size} RATE={rate:.2f}/s "
                f"ELAPSED={elapsed:.0f}s ETA={eta_s:.0f}s FREE_DISK_GB={free_gb:.1f} RESULT={result} ERRORS={errors}"
            )

        _log(f"STATUS: COLLECTION_COMPLETED PARTITIONS={total} TOTAL_RECORDS={total_records} TOTAL_BYTES_DOWNLOADED={total_bytes} ERRORS={errors}")
        return 0 if errors == 0 else 1
    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
