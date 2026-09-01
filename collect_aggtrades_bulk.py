"""Bulk aggTrades collector using Binance's official historical data archive
(data.binance.vision) instead of paginated REST calls -- orders of magnitude
faster for multi-year backfills. REST (BinanceMarketDataClient) is reserved
for gaps/very recent days not yet published as bulk files.

Storage: partitioned, columnar, compressed Parquet (not one giant gzip):
    data/aggtrades/{SYMBOL}/{YEAR}/{MONTH}/{SYMBOL}_{YEAR}_{MONTH}.parquet
    data/aggtrades/{SYMBOL}/{YEAR}/{MONTH}/{SYMBOL}_{YEAR}_{MONTH}_DD.parquet  (daily, current month)

Restart-safe: a manifest (data/aggtrades/manifest.json) records one entry per
partition with VALIDATED=YES only after integrity checks pass; completed
partitions are never re-downloaded. Protected by a process lock (same
pattern as collect_aggtrades.py).

Disk-safety gate: aborts BEFORE starting a new partition if free disk space
drops below MIN_FREE_DISK_GB. Each partition is downloaded, parsed, written
to Parquet, validated, and the temporary zip is deleted immediately -- peak
extra disk usage per partition is ~1 zip, not the whole dataset.

Does not touch MySQL / the operational database. Does not touch Paper Live.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from utils.helpers import retry

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = BASE_DIR / "data" / "aggtrades"
MANIFEST_PATH = OUT_ROOT / "manifest.json"
LOCK_PATH = BASE_DIR / "data" / "aggtrades_bulk.lock"
TMP_DIR = OUT_ROOT / "_tmp"

SYMBOLS = ("BTCUSDT", "ETHUSDT")
DATE_START = date(2024, 1, 1)
MIN_FREE_DISK_GB = 15.0  # hard abort threshold; user's own past incident was disk exhaustion
BULK_BASE_URL = "https://data.binance.vision/data/spot"
COLUMNS = ["a", "p", "q", "f", "l", "T", "m", "M"]


def _log(message: str) -> None:
    print(message, flush=True)


def _acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = LOCK_PATH.read_text(encoding="utf-8").strip() if LOCK_PATH.exists() else "?"
        raise RuntimeError(f"Lock {LOCK_PATH} exists (pid={existing}). Another bulk collector instance may be running.")
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
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


@dataclass(frozen=True)
class Partition:
    symbol: str
    period: str  # "2024-01" (monthly) or "2026-08-15" (daily)
    granularity: str  # "monthly" | "daily"

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.period}"

    @property
    def url(self) -> str:
        if self.granularity == "monthly":
            return f"{BULK_BASE_URL}/monthly/aggTrades/{self.symbol}/{self.symbol}-aggTrades-{self.period}.zip"
        return f"{BULK_BASE_URL}/daily/aggTrades/{self.symbol}/{self.symbol}-aggTrades-{self.period}.zip"

    @property
    def output_path(self) -> Path:
        if self.granularity == "monthly":
            year, month = self.period.split("-")
            return OUT_ROOT / self.symbol / year / month / f"{self.symbol}_{year}_{month}.parquet"
        year, month, day = self.period.split("-")
        return OUT_ROOT / self.symbol / year / month / f"{self.symbol}_{year}_{month}_{day}.parquet"


def _monthly_periods(start: date, end_exclusive: date) -> list[str]:
    periods = []
    cursor = date(start.year, start.month, 1)
    while cursor < end_exclusive:
        periods.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return periods


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


def _discover_partitions() -> list[Partition]:
    """Monthly files for fully-elapsed months; daily files to fill the
    current (possibly incomplete) month up to the latest published day."""
    today = datetime.now(timezone.utc).date()
    current_month_start = date(today.year, today.month, 1)
    partitions: list[Partition] = []
    for symbol in SYMBOLS:
        for period in _monthly_periods(DATE_START, current_month_start):
            partitions.append(Partition(symbol, period, "monthly"))
        cursor = current_month_start
        while cursor <= today:
            partitions.append(Partition(symbol, cursor.isoformat(), "daily"))
            cursor += timedelta(days=1)
    return partitions


def _parse_aggtrades_zip(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise RuntimeError(f"Unexpected archive contents in {zip_path}: {names}")
        with archive.open(names[0]) as csv_handle:
            head = csv_handle.read(64)
            has_header = head.startswith(b"agg_trade_id") or head.startswith(b"a,p,q")
        with archive.open(names[0]) as csv_handle:
            df = pd.read_csv(
                csv_handle,
                header=0 if has_header else None,
                names=COLUMNS,
                dtype={"a": "int64", "f": "int64", "l": "int64", "T": "int64"},
            )
    df["p"] = pd.to_numeric(df["p"], errors="coerce")
    df["q"] = pd.to_numeric(df["q"], errors="coerce")
    df["m"] = df["m"].astype(bool)
    # Binance bulk archives switched aggTrades timestamps from milliseconds to
    # microseconds at some point in 2025-2026; normalize to milliseconds so
    # this dataset is comparable with the earlier REST-collected 2025-01 data.
    if len(df) and int(df["T"].iloc[0]) > 10**14:
        df["T"] = df["T"] // 1000
    return df


def _validate_partition(df: pd.DataFrame, partition: Partition) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if df.empty:
        return False, ["empty_dataframe"]
    if df["a"].duplicated().any():
        issues.append(f"duplicate_agg_trade_ids={int(df['a'].duplicated().sum())}")
    if not df["a"].is_monotonic_increasing:
        issues.append("agg_trade_id_not_sorted")
    if not df["T"].is_monotonic_increasing:
        issues.append("timestamp_not_sorted")
    if (df["p"] <= 0).any():
        issues.append(f"invalid_price={int((df['p'] <= 0).sum())}")
    if (df["q"] <= 0).any():
        issues.append(f"invalid_qty={int((df['q'] <= 0).sum())}")
    if df[["a", "p", "q", "f", "l", "T", "m"]].isna().any().any():
        issues.append("missing_values")
    expected_start = datetime.strptime(partition.period[:7], "%Y-%m").replace(tzinfo=timezone.utc) if partition.granularity == "monthly" else datetime.strptime(partition.period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    first_ts = datetime.fromtimestamp(int(df["T"].iloc[0]) / 1000, tz=timezone.utc)
    if abs((first_ts - expected_start).total_seconds()) > 26 * 3600:
        issues.append(f"first_timestamp_out_of_expected_period first_ts={first_ts.isoformat()}")
    return len(issues) == 0, issues


def _hash_dataframe(df: pd.DataFrame) -> str:
    sha256 = hashlib.sha256()
    sha256.update(pd.util.hash_pandas_object(df[COLUMNS], index=False).values.tobytes())
    return sha256.hexdigest()


def process_partition(partition: Partition, manifest: dict[str, Any]) -> str:
    existing = manifest["partitions"].get(partition.key)
    if existing and existing.get("VALIDATED") == "YES" and partition.output_path.exists():
        return "SKIPPED_ALREADY_VALIDATED"

    head = _head(partition.url)
    if head.status_code == 404:
        manifest["partitions"][partition.key] = {"SYMBOL": partition.symbol, "PERIOD": partition.period, "VALIDATED": "NOT_AVAILABLE"}
        return "NOT_AVAILABLE"
    head.raise_for_status()

    free_gb = _free_disk_gb(OUT_ROOT)
    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(f"DISK_SAFETY_ABORT free_disk_gb={free_gb:.1f} < MIN_FREE_DISK_GB={MIN_FREE_DISK_GB}")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TMP_DIR / f"{partition.symbol}_{partition.period}.zip"
    try:
        _download(partition.url, zip_path)
        df = _parse_aggtrades_zip(zip_path)
        valid, issues = _validate_partition(df, partition)
        record_hash = _hash_dataframe(df)
        partition.output_path.parent.mkdir(parents=True, exist_ok=True)
        if valid:
            df[COLUMNS].to_parquet(partition.output_path, compression="zstd", index=False)
        manifest["partitions"][partition.key] = {
            "SYMBOL": partition.symbol,
            "PERIOD": partition.period,
            "SOURCE": "BINANCE_BULK",
            "RECORDS": int(len(df)),
            "FIRST_TIMESTAMP": datetime.fromtimestamp(int(df["T"].iloc[0]) / 1000, tz=timezone.utc).isoformat(),
            "LAST_TIMESTAMP": datetime.fromtimestamp(int(df["T"].iloc[-1]) / 1000, tz=timezone.utc).isoformat(),
            "HASH": record_hash,
            "VALIDATED": "YES" if valid else "NO",
            "ISSUES": issues,
            "CREATED_AT": datetime.now(timezone.utc).isoformat(),
        }
        return "VALIDATED" if valid else f"INVALID:{issues}"
    finally:
        zip_path.unlink(missing_ok=True)


def main() -> int:
    _acquire_lock()
    started = time.monotonic()
    try:
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        manifest = _load_manifest()
        partitions = _discover_partitions()
        total = len(partitions)
        completed = sum(1 for p in partitions if manifest["partitions"].get(p.key, {}).get("VALIDATED") in ("YES", "NOT_AVAILABLE"))
        _log(f"STATUS: DOWNLOADING SOURCE=BINANCE_BULK TOTAL_PARTITIONS={total} PARTITIONS_COMPLETED={completed}")

        errors = 0
        for index, partition in enumerate(partitions, start=1):
            free_gb = _free_disk_gb(OUT_ROOT)
            try:
                result = process_partition(partition, manifest)
            except Exception as exc:  # noqa: BLE001 - report and stop this pipeline only
                errors += 1
                _log(f"ERRORS: 1 PARTITION={partition.key} REASON={exc}")
                _save_manifest(manifest)
                if "DISK_SAFETY_ABORT" in str(exc):
                    _log("STATUS: ABORTED REASON=DISK_SAFETY_ABORT")
                    return 1
                continue
            _save_manifest(manifest)
            elapsed = time.monotonic() - started
            rate = index / max(elapsed, 1e-9)
            eta_s = (total - index) / max(rate, 1e-9)
            _log(
                f"STATUS: DOWNLOADING SOURCE=BINANCE_BULK SYMBOL={partition.symbol} PERIOD={partition.period} "
                f"PARTITION={index}/{total} RESULT={result} TOTAL_PROGRESS={index / total * 100:.1f}% "
                f"FREE_DISK_GB={free_gb:.1f} ELAPSED={elapsed:.0f}s ETA={eta_s:.0f}s ERRORS={errors}"
            )

        _log(f"STATUS: COLLECTION_COMPLETED PARTITIONS={total} ERRORS={errors}")
        return 0 if errors == 0 else 1
    finally:
        _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
