from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exchange.binance_market_data_client import BinanceMarketDataClient

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = BASE_DIR / "data" / "binance_spot_aggtrades_btc_eth_2025-01.jsonl.gz"
DEFAULT_CHECKPOINT = BASE_DIR / "data" / "binance_spot_aggtrades_btc_eth_2025-01.checkpoint.json"
SYMBOLS = ("BTC/USDT", "ETH/USDT")
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = datetime(2025, 2, 1, tzinfo=timezone.utc)
PAGE_LIMIT = 1000


def _write_checkpoint(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
        try:
            temp.replace(path)
        except PermissionError:
            path.write_text(temp.read_text(encoding="utf-8"), encoding="utf-8")
    finally:
        temp.unlink(missing_ok=True)


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"completed": [], "current": None}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Binance Spot aggTrades with resumable progress.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--start", default=START.isoformat())
    parser.add_argument("--end", default=END.isoformat())
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    return parser.parse_args()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _acquire_lock(path: Path) -> int:
    """Create an exclusive lock file to prevent two collector instances from
    appending to the same gzip output concurrently (root cause of the
    2025-01 corruption: interleaved 'ab' writes from overlapping processes
    corrupted the multi-member gzip stream from the second member onward)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing_pid = path.read_text(encoding="utf-8").strip() if path.exists() else "?"
        raise RuntimeError(
            f"Lock file {path} already exists (pid={existing_pid}). "
            "Another collect_aggtrades.py process may already be running. "
            "Remove the lock file only after confirming no other instance is active."
        )
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.close(fd)
    return os.getpid()


def _release_lock(path: Path) -> None:
    path.unlink(missing_ok=True)


def main() -> int:
    args = _parse_args()
    lock_path = args.output.with_suffix(args.output.suffix + ".lock")
    _acquire_lock(lock_path)
    try:
        return _run(args)
    finally:
        _release_lock(lock_path)


def _run(args: argparse.Namespace) -> int:
    args = _parse_args()
    start = _dt(args.start)
    end = _dt(args.end)
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    contexts = [(symbol, start, end) for symbol in symbols]
    checkpoint = _load_checkpoint(args.checkpoint)
    completed = {str(item) for item in checkpoint.get("completed", [])}
    started = time.monotonic()
    total_trades = int(checkpoint.get("total_trades", 0))
    client = BinanceMarketDataClient()
    print("AGGTRADES COLLECTION", flush=True)
    print(f"Period: {start.isoformat()} -> {end.isoformat()}", flush=True)
    try:
        client.connect()
        for index, (symbol, context_start, context_end) in enumerate(contexts, start=1):
            if symbol in completed:
                print(f"[{index}/{len(contexts)}] {symbol} SKIPPED (completed)", flush=True)
                continue
            current = checkpoint.get("current") or {}
            from_id = int(current["from_id"]) if current.get("symbol") == symbol else None
            context_count = int(current.get("context_trades", 0)) if current.get("symbol") == symbol else 0
            context_started = time.monotonic()
            last_status = context_started
            print(f"\n[{index}/{len(contexts)}] {symbol}", flush=True)
            print("Status: RUNNING", flush=True)
            with gzip.open(args.output, "ab") as output:
                while True:
                    try:
                        page = client.fetch_aggtrades_page(symbol, context_start, context_end, from_id=from_id, limit=PAGE_LIMIT)
                    except Exception as exc:
                        print(f"RETRYING {symbol}: {exc}", flush=True)
                        time.sleep(5)
                        continue
                    if not page:
                        break
                    for row in page:
                        if int(row["T"]) < int(context_start.timestamp() * 1000) or int(row["T"]) >= int(context_end.timestamp() * 1000):
                            continue
                        output.write((json.dumps({"symbol": symbol, **row}, separators=(",", ":")) + "\n").encode("utf-8"))
                        context_count += 1
                        total_trades += 1
                    last_id = int(page[-1]["a"])
                    from_id = last_id + 1
                    checkpoint["current"] = {"symbol": symbol, "from_id": from_id, "context_trades": context_count}
                    checkpoint["total_trades"] = total_trades
                    _write_checkpoint(args.checkpoint, checkpoint)
                    now = time.monotonic()
                    if now - last_status >= 5:
                        rate = context_count / max(now - context_started, 1e-9)
                        print(f"Trades downloaded: {total_trades} | Current timestamp: {datetime.fromtimestamp(int(page[-1]['T']) / 1000, timezone.utc).isoformat()}", flush=True)
                        print(f"Context progress: {min(99.9, (int(page[-1]['T']) - int(context_start.timestamp() * 1000)) / max(1, int((context_end - context_start).total_seconds() * 1000)) * 100):.1f}% | Rate: {rate:.0f} aggTrades/s | Contexts: {len(completed)}/{len(contexts)}", flush=True)
                        last_status = now
                    if len(page) < PAGE_LIMIT or int(page[-1]["T"]) >= int(context_end.timestamp() * 1000):
                        break
            completed.add(symbol)
            checkpoint["completed"] = sorted(completed)
            checkpoint["current"] = None
            checkpoint["total_trades"] = total_trades
            _write_checkpoint(args.checkpoint, checkpoint)
            print(f"[{index}/{len(contexts)}] {symbol} COMPLETED | Trades: {context_count} | Duration: {time.monotonic() - context_started:.0f}s", flush=True)
    finally:
        client.disconnect()
    print("COLLECTION_COMPLETED" if len(completed) == len(contexts) else "COLLECTION_FINISHED_WITH_ERRORS", flush=True)
    print(f"CONTEXTS_COMPLETED: {len(completed)}/{len(contexts)}", flush=True)
    print(f"TOTAL_AGGTRADES: {total_trades}", flush=True)
    print(f"OUTPUT_FILE: {args.output}", flush=True)
    print(f"TOTAL_DURATION: {time.monotonic() - started:.0f}s", flush=True)
    return 0 if len(completed) == len(contexts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
