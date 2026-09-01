from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from exchange.binance_market_data_client import BinanceMarketDataClient

BASE_DIR = Path(__file__).resolve().parent
OUTPUT = BASE_DIR / "data" / "binance_spot_extended_klines.csv.gz"
SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
TIMEFRAMES = ("5m", "15m", "1h")
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 18, 18, tzinfo=timezone.utc)
COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trade_count", "taker_buy_base_volume", "taker_buy_quote_volume",
    "symbol", "timeframe",
)


def _load_completed() -> tuple[pd.DataFrame, set[tuple[str, str]]]:
    if not OUTPUT.exists():
        return pd.DataFrame(columns=COLUMNS), set()
    frame = pd.read_csv(OUTPUT, compression="gzip", parse_dates=["open_time", "close_time"])
    completed = set(zip(frame["symbol"].astype(str), frame["timeframe"].astype(str)))
    return frame, completed


def _write_atomic(frame: pd.DataFrame) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp.csv.gz")
    frame = frame.sort_values(["symbol", "timeframe", "open_time"])
    frame = frame.drop_duplicates(["symbol", "timeframe", "open_time"], keep="last")
    frame.to_csv(temp, index=False, compression="gzip")
    temp.replace(OUTPUT)


def main() -> int:
    started = time.monotonic()
    contexts = [(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES]
    combined, completed = _load_completed()
    client = BinanceMarketDataClient()
    failed: list[tuple[str, str, str]] = []
    try:
        client.connect()
        for index, (symbol, timeframe) in enumerate(contexts, start=1):
            if (symbol, timeframe) in completed:
                print(f"[{index}/12] {symbol} {timeframe} SKIPPED (already completed)", flush=True)
                continue
            context_started = time.monotonic()
            print(f"[{index}/12] {symbol} {timeframe}", flush=True)
            print("Status: DOWNLOADING", flush=True)
            try:
                frame = client.fetch_extended_klines(symbol, timeframe, START, END).reset_index()
                frame["symbol"] = symbol
                frame["timeframe"] = timeframe
                frame = frame[list(COLUMNS)]
                combined = pd.concat([combined, frame], ignore_index=True)
                _write_atomic(combined)
                completed.add((symbol, timeframe))
                elapsed = time.monotonic() - context_started
                total_elapsed = time.monotonic() - started
                print(f"[{index}/12] {symbol} {timeframe} COMPLETED", flush=True)
                print(f"Linhas: {len(frame)}", flush=True)
                print(f"Tempo: {elapsed:.0f}s", flush=True)
                print(f"Progresso total: {len(completed) / len(contexts) * 100:.1f}%", flush=True)
                print(f"Tempo decorrido: {total_elapsed:.0f}s", flush=True)
            except Exception as exc:
                failed.append((symbol, timeframe, str(exc)))
                print("ERROR", flush=True)
                print(f"SYMBOL: {symbol}", flush=True)
                print(f"TIMEFRAME: {timeframe}", flush=True)
                print(f"MESSAGE: {exc}", flush=True)
    finally:
        client.disconnect()

    final = pd.read_csv(OUTPUT, compression="gzip", parse_dates=["open_time", "close_time"]) if OUTPUT.exists() else pd.DataFrame(columns=COLUMNS)
    duplicate_count = int(final.duplicated(["symbol", "timeframe", "open_time"]).sum())
    gaps = 0
    for (symbol, timeframe), group in final.groupby(["symbol", "timeframe"]):
        expected = pd.Timedelta(timeframe)
        gaps += int((group.sort_values("open_time")["open_time"].diff() > expected).sum())
    size_gb = OUTPUT.stat().st_size / (1024 ** 3) if OUTPUT.exists() else 0.0
    print("COLLECTION_COMPLETED" if len(completed) == len(contexts) and not failed else "COLLECTION_FINISHED_WITH_ERRORS", flush=True)
    print(f"CONTEXTS_COMPLETED: {len(completed)}/12", flush=True)
    print(f"TOTAL_ROWS: {len(final)}", flush=True)
    print(f"OUTPUT_FILE: {OUTPUT}", flush=True)
    print(f"OUTPUT_SIZE_GB: {size_gb:.3f}", flush=True)
    print(f"START_TIMESTAMP: {final['open_time'].min() if not final.empty else None}", flush=True)
    print(f"END_TIMESTAMP: {final['open_time'].max() if not final.empty else None}", flush=True)
    print(f"DUPLICATES: {duplicate_count}", flush=True)
    print(f"GAPS: {gaps}", flush=True)
    print(f"TOTAL_DURATION: {time.monotonic() - started:.0f}s", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
