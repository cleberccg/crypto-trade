"""Validate the consolidated Binance Spot aggTrades dataset (streaming, single pass).

Reused by the microstructure Discovery hypothesis; not part of BacktestEngine,
RiskManager or Paper Live. Read-only validation utility. Uses stdlib gzip
(GzipFile), which verifies the per-member CRC32/ISIZE trailer on read, so any
corrupted or truncated member surfaces as BadGzipFile/zlib.error here.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "binance_spot_aggtrades_btc_eth_2025-01.jsonl.gz"
CHECKPOINT_PATH = BASE_DIR / "data" / "binance_spot_aggtrades_btc_eth_2025-01.checkpoint.json"
STATUS_EVERY = 5_000_000
TOTAL_EXPECTED = 87_547_520
EXPECTED_SYMBOLS = ("BTC/USDT", "ETH/USDT")
EXPECTED_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
EXPECTED_END = datetime(2025, 2, 1, tzinfo=timezone.utc)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _new_symbol_stats() -> dict[str, Any]:
    return {
        "count": 0,
        "min_id": None,
        "max_id": None,
        "min_ts": None,
        "max_ts": None,
        "last_ts": None,
        "last_id": None,
        "out_of_order_ts": 0,
        "non_increasing_id": 0,
        "id_gaps": 0,
        "invalid_price": 0,
        "invalid_qty": 0,
        "missing_fields": 0,
        "maker_true": 0,
        "maker_false": 0,
    }


def run_validation() -> dict[str, Any]:
    sha256 = hashlib.sha256()
    per_symbol: dict[str, dict[str, Any]] = defaultdict(_new_symbol_stats)
    total_lines = 0
    invalid_json = 0
    required = ("a", "p", "q", "f", "l", "T", "m", "symbol")
    started = time.monotonic()

    print("STATUS: RUNNING CURRENT_STAGE=GZIP_STREAM_VALIDATION", flush=True)
    with gzip.open(DATA_PATH, "rb") as handle:
        for raw_line in handle:
            sha256.update(raw_line)
            total_lines += 1
            if total_lines % STATUS_EVERY == 0:
                elapsed = time.monotonic() - started
                rate = total_lines / max(elapsed, 1e-9)
                eta = max(0.0, (TOTAL_EXPECTED - total_lines) / max(rate, 1e-9))
                print(
                    f"STATUS: RUNNING CURRENT_STAGE=GZIP_STREAM_VALIDATION "
                    f"PROCESSED={total_lines} TOTAL={TOTAL_EXPECTED} "
                    f"PROGRESS={total_lines / TOTAL_EXPECTED * 100:.1f}% "
                    f"RATE={rate:.0f}/s ELAPSED={elapsed:.0f}s ETA={eta:.0f}s",
                    flush=True,
                )
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                invalid_json += 1
                continue
            symbol = row.get("symbol")
            stats = per_symbol[symbol]
            if any(field not in row for field in required):
                stats["missing_fields"] += 1
                continue
            trade_id = int(row["a"])
            price = float(row["p"])
            qty = float(row["q"])
            ts = int(row["T"])
            maker = bool(row["m"])

            if price <= 0:
                stats["invalid_price"] += 1
            if qty <= 0:
                stats["invalid_qty"] += 1

            if stats["min_id"] is None or trade_id < stats["min_id"]:
                stats["min_id"] = trade_id
            if stats["max_id"] is None or trade_id > stats["max_id"]:
                stats["max_id"] = trade_id
            if stats["min_ts"] is None or ts < stats["min_ts"]:
                stats["min_ts"] = ts
            if stats["max_ts"] is None or ts > stats["max_ts"]:
                stats["max_ts"] = ts

            if stats["last_ts"] is not None and ts < stats["last_ts"]:
                stats["out_of_order_ts"] += 1
            if stats["last_id"] is not None:
                if trade_id <= stats["last_id"]:
                    # aggTrade ids are unique/sequential per symbol; a non-increasing
                    # id here means either a duplicate row or an out-of-order write.
                    stats["non_increasing_id"] += 1
                elif trade_id > stats["last_id"] + 1:
                    stats["id_gaps"] += 1
            stats["last_ts"] = ts
            stats["last_id"] = trade_id

            if maker:
                stats["maker_true"] += 1
            else:
                stats["maker_false"] += 1
            stats["count"] += 1

    elapsed = time.monotonic() - started
    print(f"STATUS: COMPLETED CURRENT_STAGE=GZIP_STREAM_VALIDATION ELAPSED={elapsed:.0f}s", flush=True)

    return {
        "sha256": sha256.hexdigest(),
        "total_lines": total_lines,
        "invalid_json": invalid_json,
        "per_symbol": dict(per_symbol),
    }


def _validate_checkpoint() -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not CHECKPOINT_PATH.exists():
        return False, ["checkpoint file missing"]
    checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    completed = set(checkpoint.get("completed", []))
    for symbol in EXPECTED_SYMBOLS:
        if symbol not in completed:
            issues.append(f"{symbol} not marked completed in checkpoint")
    if checkpoint.get("current") is not None:
        issues.append(f"checkpoint has an incomplete context: {checkpoint.get('current')}")
    if checkpoint.get("total_trades") != TOTAL_EXPECTED:
        issues.append(f"checkpoint total_trades={checkpoint.get('total_trades')} != expected {TOTAL_EXPECTED}")
    return len(issues) == 0, issues


def main() -> int:
    if not DATA_PATH.exists():
        print(f"DATASET_VALID: NO (file not found: {DATA_PATH})")
        return 1

    try:
        result = run_validation()
    except (OSError, EOFError) as exc:  # gzip.BadGzipFile is an OSError subclass
        print(f"GZIP_VALID: NO ({type(exc).__name__}: {exc})")
        print("DATASET_VALID: NO")
        return 1

    per_symbol = result["per_symbol"]
    total_lines = result["total_lines"]
    invalid_json = result["invalid_json"]

    problems: list[str] = []
    if invalid_json:
        problems.append(f"invalid_json={invalid_json}")
    if total_lines != TOTAL_EXPECTED:
        problems.append(f"total_lines={total_lines} != TOTAL_EXPECTED={TOTAL_EXPECTED}")
    for symbol in EXPECTED_SYMBOLS:
        if symbol not in per_symbol:
            problems.append(f"missing symbol {symbol}")
    for symbol, stats in per_symbol.items():
        if stats["out_of_order_ts"]:
            problems.append(f"{symbol} out_of_order_ts={stats['out_of_order_ts']}")
        if stats["non_increasing_id"]:
            problems.append(f"{symbol} non_increasing_id_or_duplicate={stats['non_increasing_id']}")
        if stats["invalid_price"]:
            problems.append(f"{symbol} invalid_price={stats['invalid_price']}")
        if stats["invalid_qty"]:
            problems.append(f"{symbol} invalid_qty={stats['invalid_qty']}")
        if stats["missing_fields"]:
            problems.append(f"{symbol} missing_fields={stats['missing_fields']}")
        min_ts, max_ts = stats["min_ts"], stats["max_ts"]
        if min_ts is not None and min_ts > int(EXPECTED_START.timestamp() * 1000) + 60_000:
            problems.append(f"{symbol} first timestamp {_iso(min_ts)} later than expected start")
        if max_ts is not None and max_ts < int(EXPECTED_END.timestamp() * 1000) - 60_000:
            problems.append(f"{symbol} last timestamp {_iso(max_ts)} earlier than expected end")

    checkpoint_valid, checkpoint_issues = _validate_checkpoint()
    problems.extend(checkpoint_issues)

    print(f"\nDATASET_PATH: {DATA_PATH}")
    print(f"FILE_SIZE_BYTES: {DATA_PATH.stat().st_size}")
    print(f"SHA256: {result['sha256']}")
    print(f"TOTAL_LINES: {total_lines}")
    print(f"INVALID_JSON: {invalid_json}")
    for symbol, stats in sorted(per_symbol.items()):
        print(f"\n--- {symbol} ---")
        print(f"count={stats['count']}")
        print(f"min_id={stats['min_id']} max_id={stats['max_id']}")
        print(f"first_ts={_iso(stats['min_ts'])} last_ts={_iso(stats['max_ts'])}")
        print(f"out_of_order_ts={stats['out_of_order_ts']}")
        print(f"non_increasing_id_or_duplicate={stats['non_increasing_id']}")
        print(f"id_gaps(non-contiguous a; informational only, not necessarily corruption)={stats['id_gaps']}")
        print(f"invalid_price={stats['invalid_price']} invalid_qty={stats['invalid_qty']} missing_fields={stats['missing_fields']}")
        print(f"maker_true={stats['maker_true']} maker_false={stats['maker_false']}")

    print(f"\nCHECKPOINT_VALID: {'YES' if checkpoint_valid else 'NO'}")
    for issue in checkpoint_issues:
        print(f"CHECKPOINT_ISSUE: {issue}")

    dataset_valid = len(problems) == 0
    print(f"\nGZIP_VALID: YES")
    print(f"DATASET_VALID: {'YES' if dataset_valid else 'NO'}")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 0 if dataset_valid else 1


if __name__ == "__main__":
    sys.exit(main())
