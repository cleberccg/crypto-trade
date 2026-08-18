from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt
from sqlalchemy import text

from database.connection import get_session

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "optimization" / "results" / "market_regime_router_phase18_5A_preparation"

TIMEFRAME_MINUTES = {
    "5m": 5,
    "15m": 15,
    "1h": 60,
}


@dataclass(frozen=True)
class PrepConfig:
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    min_start: datetime
    train_months: int
    test_months: int
    backfill_mode: str
    output_prefix: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FASE 18.5A - Preparacao de base historica para rolling OOS completo",
    )
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    parser.add_argument("--timeframes", default="5m,15m,1h")
    parser.add_argument("--min-start", default="2024-01-01")
    parser.add_argument("--train-months", type=int, default=4)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument(
        "--backfill-mode",
        choices=["deficient-only", "full-matrix"],
        default="deficient-only",
        help="deficient-only backfills only contexts starting after --min-start; full-matrix fetches all contexts from --min-start",
    )
    parser.add_argument("--output-prefix", default="phase18_5A_preparation")
    return parser.parse_args()


def dt_utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def parse_date_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(dt: datetime, months: int) -> datetime:
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return str(value)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_coverage_matrix(symbols: tuple[str, ...], timeframes: tuple[str, ...]) -> list[dict[str, Any]]:
    query = text(
        """
        SELECT
            symbol,
            timeframe,
            MIN(open_time) AS min_open_time,
            MAX(open_time) AS max_open_time,
            COUNT(*) AS candles_count,
            COUNT(*) - COUNT(DISTINCT open_time) AS duplicate_count
        FROM candles
        WHERE symbol IN :symbols
          AND timeframe IN :timeframes
        GROUP BY symbol, timeframe
        """
    ).bindparams(symbols=symbols, timeframes=timeframes)

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    with get_session() as session:
        rows = session.execute(query).mappings().all()
        for row in rows:
            key = (str(row["symbol"]), str(row["timeframe"]))
            by_key[key] = {
                "symbol": key[0],
                "timeframe": key[1],
                "min_open_time": ensure_utc(row["min_open_time"]),
                "max_open_time": ensure_utc(row["max_open_time"]),
                "candles_count": int(row["candles_count"] or 0),
                "duplicate_count": int(row["duplicate_count"] or 0),
            }

    out: list[dict[str, Any]] = []
    for s in symbols:
        for tf in timeframes:
            row = by_key.get((s, tf))
            if row is None:
                out.append(
                    {
                        "symbol": s,
                        "timeframe": tf,
                        "min_open_time": None,
                        "max_open_time": None,
                        "candles_count": 0,
                        "duplicate_count": 0,
                    }
                )
            else:
                out.append(row)
    return out


def timeframe_to_ms(timeframe: str) -> int:
    mins = TIMEFRAME_MINUTES.get(timeframe)
    if mins is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return mins * 60 * 1000


def fetch_and_upsert(symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    limit = 1000
    tf_ms = timeframe_to_ms(timeframe)
    cursor = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    fetched = 0
    inserted_or_updated = 0
    api_calls = 0

    stmt = text(
        """
        INSERT INTO candles (
            symbol, timeframe, open_time, open, high, low, close, volume, close_time
        ) VALUES (
            :symbol, :timeframe, :open_time, :open, :high, :low, :close, :volume, :close_time
        )
        ON DUPLICATE KEY UPDATE
            open = VALUES(open),
            high = VALUES(high),
            low = VALUES(low),
            close = VALUES(close),
            volume = VALUES(volume),
            close_time = VALUES(close_time)
        """
    )

    while cursor <= end_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
        api_calls += 1
        if not batch:
            break

        normalized: list[dict[str, Any]] = []
        seen_ts: set[int] = set()
        for row in batch:
            ts = int(row[0])
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            if ts < cursor:
                continue
            if ts > end_ms:
                continue
            open_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            close_time = open_time + timedelta(milliseconds=tf_ms)
            normalized.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "open_time": open_time,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": close_time,
                }
            )

        if normalized:
            with get_session() as session:
                session.execute(stmt, normalized)
                session.commit()
            inserted_or_updated += len(normalized)
            fetched += len(normalized)

        last_ts = int(batch[-1][0])
        next_cursor = last_ts + tf_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor

        if len(batch) < limit:
            break

        time.sleep(0.08)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "api_calls": api_calls,
        "rows_fetched": fetched,
        "rows_upserted": inserted_or_updated,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }


def detect_gaps_and_order(symbol: str, timeframe: str, min_open_time: datetime | None = None) -> dict[str, Any]:
    tf_minutes = TIMEFRAME_MINUTES[timeframe]
    expected = timedelta(minutes=tf_minutes)
    if min_open_time is None:
        q = text(
            """
            SELECT open_time
            FROM candles
            WHERE symbol = :symbol AND timeframe = :timeframe
            ORDER BY open_time ASC
            """
        )
        params = {"symbol": symbol, "timeframe": timeframe}
    else:
        q = text(
            """
            SELECT open_time
            FROM candles
            WHERE symbol = :symbol AND timeframe = :timeframe AND open_time >= :min_open_time
            ORDER BY open_time ASC
            """
        )
        params = {"symbol": symbol, "timeframe": timeframe, "min_open_time": min_open_time}

    with get_session() as session:
        times = [ensure_utc(row[0]) for row in session.execute(q, params).all()]

    if not times:
        return {
            "gap_count": 0,
            "missing_candles_estimated": 0,
            "max_gap_minutes": 0.0,
            "out_of_order_count": 0,
        }

    gap_count = 0
    missing = 0
    max_gap_minutes = 0.0
    out_of_order = 0

    prev = times[0]
    for curr in times[1:]:
        delta = curr - prev
        if delta.total_seconds() <= 0:
            out_of_order += 1
        if delta > expected:
            gap_count += 1
            miss = int(round(delta.total_seconds() / expected.total_seconds())) - 1
            missing += max(0, miss)
            max_gap_minutes = max(max_gap_minutes, delta.total_seconds() / 60.0)
        prev = curr

    return {
        "gap_count": gap_count,
        "missing_candles_estimated": missing,
        "max_gap_minutes": round(max_gap_minutes, 4),
        "out_of_order_count": out_of_order,
    }


def detect_corrupted_rows(symbol: str, timeframe: str) -> int:
    q = text(
        """
        SELECT COUNT(*) AS corrupted
        FROM candles
        WHERE symbol = :symbol
          AND timeframe = :timeframe
          AND (
            open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL
            OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
            OR high < low
          )
        """
    )
    with get_session() as session:
        val = session.execute(q, {"symbol": symbol, "timeframe": timeframe}).scalar_one()
    return int(val or 0)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for row in rows for k in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def calculate_rolling_windows(start_dt: datetime, end_dt: datetime, train_months: int, test_months: int) -> int:
    anchor = month_start(start_dt)
    if start_dt > anchor:
        anchor = add_months(anchor, 1)

    test_start = add_months(anchor, train_months)
    windows = 0
    while True:
        test_end = add_months(test_start, test_months)
        if test_end > end_dt:
            break
        windows += 1
        test_start = add_months(test_start, 1)
    return windows


def main() -> None:
    args = parse_args()
    symbols = tuple([x.strip() for x in str(args.symbols).split(",") if x.strip()])
    timeframes = tuple([x.strip() for x in str(args.timeframes).split(",") if x.strip()])

    cfg = PrepConfig(
        symbols=symbols,
        timeframes=timeframes,
        min_start=parse_date_utc(str(args.min_start)),
        train_months=max(1, int(args.train_months)),
        test_months=max(1, int(args.test_months)),
        backfill_mode=str(args.backfill_mode),
        output_prefix=str(args.output_prefix),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    before_cov = get_coverage_matrix(cfg.symbols, cfg.timeframes)

    now = datetime.now(tz=timezone.utc)
    download_logs: list[dict[str, Any]] = []

    for row in before_cov:
        symbol = str(row["symbol"])
        timeframe = str(row["timeframe"])
        min_open = row["min_open_time"]

        should_backfill = cfg.backfill_mode == "full-matrix"
        if cfg.backfill_mode == "deficient-only":
            should_backfill = min_open is None or min_open > cfg.min_start

        if not should_backfill:
            continue

        start_dt = cfg.min_start
        log = fetch_and_upsert(symbol, timeframe, start_dt, now)
        download_logs.append(log)

    after_cov = get_coverage_matrix(cfg.symbols, cfg.timeframes)

    coverage_rows: list[dict[str, Any]] = []
    integrity_rows: list[dict[str, Any]] = []
    eligibility_rows: list[dict[str, Any]] = []
    consolidated_rows: list[dict[str, Any]] = []

    # Build context integrity and eligibility.
    for row in after_cov:
        symbol = str(row["symbol"])
        timeframe = str(row["timeframe"])
        min_open = row["min_open_time"]
        max_open = row["max_open_time"]
        count = int(row["candles_count"])
        dup = int(row["duplicate_count"])

        coverage_days = None
        if min_open and max_open:
            coverage_days = (max_open - min_open).total_seconds() / 86400.0

        gap_info = detect_gaps_and_order(symbol, timeframe)
        gap_info_since_start = detect_gaps_and_order(symbol, timeframe, cfg.min_start)
        corrupted = detect_corrupted_rows(symbol, timeframe)

        coverage_row = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": _safe_iso(min_open),
            "end": _safe_iso(max_open),
            "candles_count": count,
            "coverage_days": round(coverage_days, 4) if coverage_days is not None else None,
        }
        coverage_rows.append(coverage_row)

        integrity_row = {
            "symbol": symbol,
            "timeframe": timeframe,
            "duplicate_count": dup,
            "gap_count": int(gap_info["gap_count"]),
            "missing_candles_estimated": int(gap_info["missing_candles_estimated"]),
            "max_gap_minutes": float(gap_info["max_gap_minutes"]),
            "gap_count_since_min_start": int(gap_info_since_start["gap_count"]),
            "missing_candles_estimated_since_min_start": int(gap_info_since_start["missing_candles_estimated"]),
            "max_gap_minutes_since_min_start": float(gap_info_since_start["max_gap_minutes"]),
            "out_of_order_count": int(gap_info["out_of_order_count"]),
            "corrupted_rows": int(corrupted),
        }
        integrity_rows.append(integrity_row)

        reasons: list[str] = []
        if min_open is None or max_open is None:
            reasons.append("no_data")
        else:
            if min_open > cfg.min_start:
                reasons.append("starts_after_required_min_start")
            possible_windows = calculate_rolling_windows(min_open, max_open, cfg.train_months, cfg.test_months)
            if possible_windows < 2:
                reasons.append(f"insufficient_rolling_windows:{possible_windows}")
        if dup > 0:
            reasons.append(f"duplicates:{dup}")
        if int(gap_info["out_of_order_count"]) > 0:
            reasons.append(f"out_of_order:{int(gap_info['out_of_order_count'])}")
        if int(corrupted) > 0:
            reasons.append(f"corrupted_rows:{int(corrupted)}")

        # Relevant gap rule inside scientific horizon (>= min_start).
        tf_minutes = TIMEFRAME_MINUTES[timeframe]
        critical_gap_minutes = tf_minutes * 24 * 3
        if float(gap_info_since_start["max_gap_minutes"]) > float(critical_gap_minutes):
            reasons.append(
                f"relevant_gap_since_min_start:max_gap_minutes={float(gap_info_since_start['max_gap_minutes'])}"
            )

        eligible = len(reasons) == 0
        eligibility_rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "eligible": eligible,
                "reason": "ok" if eligible else ";".join(reasons),
            }
        )

        consolidated_rows.append(
            {
                **coverage_row,
                **integrity_row,
                "eligible": eligible,
                "eligibility_reason": "ok" if eligible else ";".join(reasons),
            }
        )

    # Matrix-level consistency and full eligibility.
    valid_ranges = [r for r in after_cov if r["min_open_time"] is not None and r["max_open_time"] is not None]
    if valid_ranges:
        matrix_start = max(r["min_open_time"] for r in valid_ranges)
        matrix_end = min(r["max_open_time"] for r in valid_ranges)
        matrix_windows = calculate_rolling_windows(matrix_start, matrix_end, cfg.train_months, cfg.test_months)
    else:
        matrix_start = None
        matrix_end = None
        matrix_windows = 0

    all_contexts_eligible = all(bool(r["eligible"]) for r in eligibility_rows) if eligibility_rows else False
    matrix_eligible = all_contexts_eligible and matrix_windows >= 2

    if matrix_eligible:
        final_message = (
            "Preparacao concluida.\n"
            "A base historica atende aos requisitos cientificos.\n"
            "O laboratorio esta apto para executar a campanha oficial da FASE 18.5 Rolling Out-of-Sample completa."
        )
    else:
        final_message = (
            "Preparacao concluida com pendencias.\n"
            "A matriz completa ainda nao atende todos os requisitos para a campanha oficial da FASE 18.5."
        )

    # Consistency diagnostics between assets/timeframes.
    starts = [r["min_open_time"] for r in after_cov if r["min_open_time"] is not None]
    ends = [r["max_open_time"] for r in after_cov if r["max_open_time"] is not None]
    counts = [int(r["candles_count"]) for r in after_cov]
    consistency = {
        "start_spread_days": round((max(starts) - min(starts)).total_seconds() / 86400.0, 4) if starts else None,
        "end_spread_days": round((max(ends) - min(ends)).total_seconds() / 86400.0, 4) if ends else None,
        "count_min": min(counts) if counts else None,
        "count_max": max(counts) if counts else None,
        "count_ratio_min_over_max": round((min(counts) / max(counts)), 6) if counts and max(counts) > 0 else None,
    }

    summary = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "phase": "FASE 18.5A - Preparacao Base Historica",
        "config": {
            "symbols": list(cfg.symbols),
            "timeframes": list(cfg.timeframes),
            "min_start": cfg.min_start.isoformat(),
            "train_months": cfg.train_months,
            "test_months": cfg.test_months,
            "backfill_mode": cfg.backfill_mode,
        },
        "download_logs": download_logs,
        "matrix": {
            "matrix_start": _safe_iso(matrix_start),
            "matrix_end": _safe_iso(matrix_end),
            "possible_rolling_windows": matrix_windows,
            "all_contexts_eligible": all_contexts_eligible,
            "matrix_eligible": matrix_eligible,
        },
        "consistency": consistency,
        "final_message": final_message,
    }

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"{cfg.output_prefix}_{stamp}.json"
    md_path = OUT_DIR / f"{cfg.output_prefix}_{stamp}.md"
    coverage_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_coverage.csv"
    integrity_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_integrity.csv"
    eligibility_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_eligibility.csv"
    consolidated_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_consolidated.csv"

    payload = {
        "summary": summary,
        "coverage": coverage_rows,
        "integrity": integrity_rows,
        "eligibility": eligibility_rows,
        "consolidated": consolidated_rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# FASE 18.5A - Preparacao da Base Historica")
    lines.append("")
    lines.append("## Objetivo")
    lines.append("Preparar e auditar a base para viabilizar a validacao Rolling OOS completa (matriz oficial).")
    lines.append("")
    lines.append("## Matriz")
    lines.append("- Ativos: " + ", ".join(cfg.symbols))
    lines.append("- Timeframes: " + ", ".join(cfg.timeframes))
    lines.append("")
    lines.append("## Cobertura temporal consolidada")
    lines.append(f"- Matrix start: {summary['matrix']['matrix_start']}")
    lines.append(f"- Matrix end: {summary['matrix']['matrix_end']}")
    lines.append(f"- Janelas Rolling possiveis (4m treino + 1m teste): {summary['matrix']['possible_rolling_windows']}")
    lines.append("")
    lines.append("## Consistencia entre ativos/timeframes")
    lines.append(f"- Dispersao de inicio (dias): {consistency['start_spread_days']}")
    lines.append(f"- Dispersao de fim (dias): {consistency['end_spread_days']}")
    lines.append(f"- Razao menor/maior contagem de candles: {consistency['count_ratio_min_over_max']}")
    lines.append("")
    lines.append("## Elegibilidade por contexto")
    for row in eligibility_rows:
        status = "Elegivel" if row["eligible"] else "Inelegivel"
        lines.append(f"- {row['symbol']} {row['timeframe']}: {status} ({row['reason']})")
    lines.append("")
    lines.append("## Integridade")
    total_gaps = sum(int(r["gap_count"]) for r in integrity_rows)
    total_dups = sum(int(r["duplicate_count"]) for r in integrity_rows)
    total_corrupted = sum(int(r["corrupted_rows"]) for r in integrity_rows)
    lines.append(f"- Total gaps: {total_gaps}")
    lines.append(f"- Total duplicates: {total_dups}")
    lines.append(f"- Total registros corrompidos: {total_corrupted}")
    lines.append("")
    lines.append("## Status final")
    lines.append(final_message)

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    write_csv(coverage_csv, coverage_rows)
    write_csv(integrity_csv, integrity_rows)
    write_csv(eligibility_csv, eligibility_rows)
    write_csv(consolidated_csv, consolidated_rows)

    print(str(json_path))
    print(str(md_path))
    print(str(coverage_csv))
    print(str(integrity_csv))
    print(str(eligibility_csv))
    print(str(consolidated_csv))


if __name__ == "__main__":
    main()
