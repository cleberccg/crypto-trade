"""Audita/repara candles armazenados comparando-os com candles fechados da Binance Spot (API pública somente leitura).

O padrão é CHECK (dry-run). O banco de dados só é alterado com --apply:
- candles CLOSED divergentes são substituídos pelos valores da Binance (mesma linha, chave única preservada);
Candles ausentes e abertos são apenas relatados; nenhuma linha é inserida ou excluída.
Linhas corretas nunca são alteradas. Nunca envia ordens.

Exemplo:
    python repair_market_data.py --symbol BNB/USDT --timeframe 4h --start 2017-11-01
    python repair_market_data.py --symbol BNB/USDT --timeframe 4h --start 2017-11-01 --apply
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from exchange.binance_market_data_client import BinanceMarketDataClient

# candles.* usa FLOAT do MySQL (precisão simples, cerca de 6 algarismos significativos): diferenças menores decorrem do arredondamento no armazenamento.
PRICE_REL_TOL = 1e-5
VOLUME_REL_TOL = PRICE_REL_TOL
COLUMNS = ["open", "high", "low", "close", "volume"]


def _naive(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert("UTC").tz_localize(None).to_pydatetime()


def fetch_binance_closed(symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp | None) -> pd.DataFrame:
    cutoff = pd.Timestamp.now(tz="UTC")
    client = BinanceMarketDataClient()
    client.connect()
    try:
        parts: list[pd.DataFrame] = []
        since = int(start.timestamp() * 1000)
        while True:
            batch = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if batch is None or batch.empty:
                break
            parts.append(batch)
            if len(batch) < 1000 or (end is not None and batch.index[-1] >= end):
                break
            since = int(batch.index[-1].timestamp() * 1000) + 1
    finally:
        client.disconnect()
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.concat(parts)
    if not frame.index.is_unique:
        raise ValueError("Duplicate Binance timestamps; repair refused")
    frame = frame.sort_index()
    frame = frame[(frame.index >= start) & (frame.index + pd.Timedelta(timeframe) <= cutoff)]
    if end is not None:
        frame = frame[frame.index < end]
    return frame[COLUMNS].astype(float)


def load_db(symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp | None) -> pd.DataFrame:
    query = "SELECT open_time,open,high,low,close,volume FROM candles WHERE symbol=:s AND timeframe=:t AND open_time>=:st"
    params = {"s": symbol, "t": timeframe, "st": _naive(start)}
    if end is not None:
        query += " AND open_time<:en"
        params["en"] = _naive(end)
    with get_session() as session:
        rows = session.execute(text(query + " ORDER BY open_time"), params).fetchall()
    return pd.DataFrame(
        [tuple(r)[1:] for r in rows],
        index=pd.DatetimeIndex([r[0] for r in rows], tz="UTC"),
        columns=COLUMNS,
    ).astype(float)


def compare(db: pd.DataFrame, ex: pd.DataFrame, timeframe: str) -> dict[str, pd.Index | pd.DataFrame]:
    now = pd.Timestamp.now(tz="UTC")
    if not db.index.is_unique or not ex.index.is_unique:
        raise ValueError("Duplicate candle timestamps; repair refused")
    ex = ex[ex.index + pd.Timedelta(timeframe) <= now]
    if not np.isfinite(ex[COLUMNS].to_numpy()).all():
        raise ValueError("Invalid Binance OHLCV; repair refused")
    joined = db.join(ex, rsuffix="_ex", how="inner")
    price_bad = joined[COLUMNS].isna().any(axis=1)
    for col in ("open", "high", "low", "close"):
        price_bad |= (joined[col] - joined[f"{col}_ex"]).abs() / joined[f"{col}_ex"].abs().clip(lower=1e-12) > PRICE_REL_TOL
    vol_bad = (joined["volume"] - joined["volume_ex"]).abs() / joined["volume_ex"].abs().clip(lower=1e-9) > VOLUME_REL_TOL
    step = pd.Timedelta(timeframe)
    diffs = db.index.to_series().diff().dropna()
    source_diffs = ex.index.to_series().diff().dropna()
    gap_ends = diffs[diffs != step].index
    return {
        "checked": joined.index,
        "divergent": joined[price_bad | vol_bad],
        "open_in_db": db.index[db.index + step > now],
        "missing_closed": ex.index.difference(db.index),
        "db_only": db.index[db.index + step <= now].difference(ex.index),
        "gap_ends": gap_ends,
        "source_gap_ends": source_diffs[source_diffs != step].index,
    }


def apply_repair(symbol: str, timeframe: str, result: dict, ex: pd.DataFrame) -> None:
    with get_session() as session:
        for ts, row in result["divergent"].iterrows():
            if ts not in ex.index or ts + pd.Timedelta(timeframe) > pd.Timestamp.now(tz="UTC"):
                raise ValueError("Repair requires a confirmed closed Binance candle")
            updated = session.execute(
                text("UPDATE candles SET open=:o,high=:h,low=:l,close=:c,volume=:v "
                     "WHERE symbol=:s AND timeframe=:t AND open_time=:ot"),
                {"o": row["open_ex"], "h": row["high_ex"], "l": row["low_ex"], "c": row["close_ex"],
                 "v": row["volume_ex"], "s": symbol, "t": timeframe, "ot": _naive(ts)},
            )
            if updated.rowcount != 1:
                raise RuntimeError("Expected exactly one candle row; transaction rolled back")


def report(result: dict, db: pd.DataFrame, label: str, show: int = 0) -> None:
    divergent = result["divergent"]
    print(f"[{label}] CANDLES_IN_DB={len(db)} CANDLES_CHECKED={len(result['checked'])} "
          f"CANDLES_DIVERGENT={len(divergent)} OPEN_CANDLES_IN_DB={len(result['open_in_db'])} "
          f"MISSING_CLOSED={len(result['missing_closed'])} DB_ONLY_NOT_ON_BINANCE={len(result['db_only'])} "
          f"GAPS={len(result['gap_ends'])} BINANCE_SOURCE_GAPS={len(result['source_gap_ends'])}")
    if len(divergent):
        print(f"[{label}] FIRST_DIVERGENT={divergent.index[0]} LAST_DIVERGENT={divergent.index[-1]}")
        for ts, row in divergent.head(show).iterrows():
            print(f"  {ts} db=" + ",".join(f"{row[c]:.8g}" for c in COLUMNS)
                  + " binance=" + ",".join(f"{row[c + '_ex']:.8g}" for c in COLUMNS))
    closed = db.index[~db.index.isin(result["open_in_db"])]
    print(f"[{label}] LATEST_CLOSED_CANDLE={closed[-1] if len(closed) else 'N/A'}")
    ready = len(result["checked"]) > 0 and all(len(result[key]) == 0 for key in
        ("divergent", "open_in_db", "missing_closed", "db_only", "gap_ends"))
    print(f"[{label}] MARKET_DATA_READY={'YES' if ready else 'NO'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="UTC date, e.g. 2017-11-01")
    parser.add_argument("--end", default="", help="Optional exclusive UTC end date")
    parser.add_argument("--show", type=int, default=0, help="Print the first N divergent rows")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Dry-run (default)")
    mode.add_argument("--apply", action="store_true", help="Write repairs to the database")
    args = parser.parse_args()
    logging.disable(logging.INFO)

    start = pd.to_datetime(args.start, utc=True)
    end = pd.to_datetime(args.end, utc=True) if args.end else pd.Timestamp.now(tz="UTC")
    if start >= end:
        parser.error("--start must precede --end/current UTC time")
    ex = fetch_binance_closed(args.symbol, args.timeframe, start, end)
    if ex.empty:
        raise RuntimeError("Empty Binance reference; repair refused")
    db = load_db(args.symbol, args.timeframe, start, end)
    result = compare(db, ex, args.timeframe)
    report(result, db, "BEFORE" if args.apply else "CHECK", args.show)

    if not args.apply:
        print("MODE=CHECK (no database change). Use --apply to repair.")
        return 0

    apply_repair(args.symbol, args.timeframe, result, ex)
    print(f"CANDLES_REPAIRED={len(result['divergent'])} OPEN_CANDLES_REMOVED=0 MISSING_INSERTED=0")
    db_after = load_db(args.symbol, args.timeframe, start, end)
    after = compare(db_after, ex, args.timeframe)
    report(after, db_after, "AFTER", args.show)
    print(f"CANDLES_STILL_DIVERGENT={len(after['divergent'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
