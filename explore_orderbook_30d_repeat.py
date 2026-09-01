"""PILOT_EXPLORATORY_ONLY -- exact repeat of explore_orderbook_pilot_richer.py
with FROZEN rules (same features, same quantiles 0.90/0.10, same entry-metrics
gate) over the expanded ~30-day window collected by collect_orderbook_depth_bulk.py
(EXPAND_30D). Only the date range and data loaders change (monthly vs daily
aggTrades partitioning). No new collection here.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from discover_microstructure_aggtrades import add_microstructure_features
from explore_orderbook_pilot_richer import (
    QUANTILE_HIGH,
    QUANTILE_LOW,
    MIN_DAYS_WITH_SIGNAL,
    _pivot_bands,
    _mid_price_proxy,
    _futures_ohlc_1m,
    _book_features_30s,
    _resample_causal_1m,
    _aggtrades_minute_bars,
    _episode_rows,
    _daily_stability,
    _magnitude_buckets,
)
from strategy_discovery_cycle1 import _entry_metrics, _passes_entry_gate

BASE_DIR = Path(__file__).resolve().parent
OUT_JSON = BASE_DIR / "explore_orderbook_30d_repeat_latest.json"
SYMBOLS = ("BTCUSDT", "ETHUSDT")

# bookDepth (futures) covers 2026-07-26..2026-08-24 (60/60 VALIDATED).
# aggTrades (spot) has no 2026-08-24 file yet -> matched window ends 08-23.
BOOKDEPTH_START = date(2026, 7, 26)
BOOKDEPTH_END = date(2026, 8, 24)
AGGTRADES_END = date(2026, 8, 23)
BOOKDEPTH_DAYS = [(BOOKDEPTH_START + timedelta(days=i)).isoformat() for i in range((BOOKDEPTH_END - BOOKDEPTH_START).days + 1)]
MATCHED_DAYS = [(BOOKDEPTH_START + timedelta(days=i)).isoformat() for i in range((AGGTRADES_END - BOOKDEPTH_START).days + 1)]


def _log(message: str) -> None:
    print(message, flush=True)


def _load_bookdepth(symbol: str, days: list[str]) -> pd.DataFrame:
    frames = []
    for day in days:
        year, month, dd = day.split("-")
        path = BASE_DIR / "data" / "orderbook_depth" / symbol / year / month / f"{symbol}_{year}_{month}_{dd}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise RuntimeError(f"No bookDepth parquet found for {symbol}")
    return pd.concat(frames, ignore_index=True).sort_values(["timestamp", "percentage"])


def _load_aggtrades(symbol: str, days: list[str]) -> pd.DataFrame:
    """Reads either the monthly (fully-elapsed month) or the daily partitions,
    matching the file layout written by collect_aggtrades_bulk.py."""
    by_month: dict[str, list[str]] = {}
    for day in days:
        year, month, _dd = day.split("-")
        by_month.setdefault(f"{year}-{month}", []).append(day)

    frames = []
    for ym, ym_days in by_month.items():
        year, month = ym.split("-")
        monthly_path = BASE_DIR / "data" / "aggtrades" / symbol / year / month / f"{symbol}_{year}_{month}.parquet"
        if monthly_path.exists():
            df = pd.read_parquet(monthly_path)
            ts = pd.to_datetime(df["T"], unit="ms", utc=True)
            start = pd.Timestamp(min(ym_days), tz="UTC")
            end_excl = pd.Timestamp(max(ym_days), tz="UTC") + pd.Timedelta(days=1)
            frames.append(df.loc[(ts >= start) & (ts < end_excl)])
            continue
        for day in ym_days:
            dd = day.split("-")[-1]
            daily_path = BASE_DIR / "data" / "aggtrades" / symbol / year / month / f"{symbol}_{year}_{month}_{dd}.parquet"
            if daily_path.exists():
                frames.append(pd.read_parquet(daily_path))
    if not frames:
        raise RuntimeError(f"No aggTrades parquet found for {symbol}")
    return pd.concat(frames, ignore_index=True).sort_values("T")


def _evaluate_feature(
    per_symbol_ohlc: dict[str, pd.DataFrame],
    per_symbol_feature: dict[str, pd.Series],
    feature_name: str,
    group_label: str,
) -> dict[str, Any]:
    all_rows = []
    for symbol in SYMBOLS:
        feat = per_symbol_feature[symbol].dropna()
        if feat.empty:
            continue
        hi = float(feat.quantile(QUANTILE_HIGH))
        lo = float(feat.quantile(QUANTILE_LOW))
        for direction, threshold in (("long_above", hi), ("long_below", lo)):
            rows = _episode_rows(per_symbol_ohlc[symbol], per_symbol_feature[symbol], direction, threshold)
            if rows.empty:
                continue
            rows["symbol"] = symbol
            rows["direction"] = direction
            rows["threshold"] = threshold
            all_rows.append(rows)
    if not all_rows:
        return {"feature": feature_name, "group": group_label, "by_horizon": [], "best": None}

    full = pd.concat(all_rows, ignore_index=True)
    by_horizon: list[dict[str, Any]] = []
    for (direction, horizon), chunk in full.groupby(["direction", "horizon"]):
        metrics = _entry_metrics(chunk["forward_return"], chunk["mfe"], chunk["mae"])
        days_present = chunk["signal_date"].nunique()
        per_symbol_episodes = chunk.groupby("symbol").size().to_dict()
        by_horizon.append(
            {
                "feature": feature_name,
                "group": group_label,
                "direction": direction,
                "threshold": float(chunk["threshold"].iloc[0]),
                "horizon": horizon,
                "days_with_episodes": int(days_present),
                "episodes_by_symbol": {k: int(v) for k, v in per_symbol_episodes.items()},
                "passes_entry_gate": bool(_passes_entry_gate(metrics)) and days_present >= MIN_DAYS_WITH_SIGNAL,
                **metrics,
            }
        )
    by_horizon.sort(key=lambda r: r["t_stat"], reverse=True)
    best = by_horizon[0] if by_horizon else None
    return {"feature": feature_name, "group": group_label, "by_horizon": by_horizon, "best": best}


def main() -> int:
    _log(f"STATUS: RUNNING CURRENT_STAGE=LOAD_30D_DATA (no new collection) BOOKDEPTH_DAYS={len(BOOKDEPTH_DAYS)} MATCHED_DAYS={len(MATCHED_DAYS)}")

    futures_ohlc: dict[str, pd.DataFrame] = {}
    book_1m: dict[str, pd.DataFrame] = {}
    spot_ohlc: dict[str, pd.DataFrame] = {}
    spot_features: dict[str, pd.DataFrame] = {}

    for symbol in SYMBOLS:
        bd = _load_bookdepth(symbol, MATCHED_DAYS)  # book features restricted to matched window for a fair comparison
        wide = _pivot_bands(bd)
        mid = _mid_price_proxy(wide)
        futures_ohlc[symbol] = _futures_ohlc_1m(mid)
        book_feats_30s = _book_features_30s(wide)
        book_1m[symbol] = _resample_causal_1m(
            book_feats_30s,
            ["book_pressure", "book_pressure_change", "book_pressure_acceleration", "liquidity_removal", "liquidity_refill", "near_vs_far_liquidity", "depth_slope_asymmetry"],
        )
        agg = _load_aggtrades(symbol, MATCHED_DAYS)
        bars = _aggtrades_minute_bars(agg)
        spot_ohlc[symbol] = bars[["open", "high", "low", "close"]]
        spot_features[symbol] = add_microstructure_features(bars)
        _log(f"STATUS: RUNNING CURRENT_STAGE=FEATURES_BUILT SYMBOL={symbol} FUTURES_BARS={len(futures_ohlc[symbol])} SPOT_BARS={len(spot_ohlc[symbol])}")

    combined_1m: dict[str, pd.DataFrame] = {}
    for symbol in SYMBOLS:
        joined = book_1m[symbol].join(spot_features[symbol][["imbalance_ratio", "flow_absorption_signal"]], how="inner")
        joined["taker_flow_z"] = (joined["imbalance_ratio"] - joined["imbalance_ratio"].rolling(60, min_periods=30).mean()) / joined["imbalance_ratio"].rolling(60, min_periods=30).std()
        joined["book_pressure_z"] = (joined["book_pressure"] - joined["book_pressure"].rolling(60, min_periods=30).mean()) / joined["book_pressure"].rolling(60, min_periods=30).std()
        joined["book_flow_divergence"] = -(joined["book_pressure_z"] * joined["taker_flow_z"])
        joined["book_taker_confirmation"] = joined["book_pressure_z"] * joined["taker_flow_z"]
        joined["absorption_book_confirmation"] = joined["flow_absorption_signal"] * joined["liquidity_removal"]
        combined_1m[symbol] = joined

    book_only_features = ["book_pressure", "book_pressure_change", "book_pressure_acceleration", "liquidity_removal", "liquidity_refill", "near_vs_far_liquidity", "depth_slope_asymmetry"]
    aggtrades_only_features = ["imbalance_ratio", "flow_absorption_signal"]
    combined_features = ["book_flow_divergence", "book_taker_confirmation", "absorption_book_confirmation"]

    report: dict[str, Any] = {"matched_days": MATCHED_DAYS, "bookdepth_days": BOOKDEPTH_DAYS, "symbols": SYMBOLS, "targets": {}}

    for target_label, ohlc_source in (("FUTURES", futures_ohlc), ("SPOT", spot_ohlc)):
        _log(f"STATUS: RUNNING CURRENT_STAGE=EVALUATE TARGET={target_label}")
        group_results: dict[str, list[dict[str, Any]]] = {"BOOK_ONLY": [], "AGGTRADES_ONLY": [], "BOOK_PLUS_AGGTRADES": []}
        daily_by_feature: dict[str, Any] = {}
        magnitude_by_feature: dict[str, Any] = {}

        for group_label, feature_list, source in (
            ("BOOK_ONLY", book_only_features, book_1m),
            ("AGGTRADES_ONLY", aggtrades_only_features, spot_features),
            ("BOOK_PLUS_AGGTRADES", combined_features, combined_1m),
        ):
            for feature_name in feature_list:
                per_symbol_feature = {s: source[s][feature_name] for s in SYMBOLS}
                result = _evaluate_feature(ohlc_source, per_symbol_feature, feature_name, group_label)
                group_results[group_label].append(result)
                best = result["best"]
                if best is None:
                    continue
                sym_rows = []
                for symbol in SYMBOLS:
                    rows = _episode_rows(ohlc_source[symbol], per_symbol_feature[symbol], best["direction"], best["threshold"])
                    if not rows.empty:
                        rows = rows[rows["horizon"] == best["horizon"]]
                        rows["symbol"] = symbol
                        sym_rows.append(rows)
                if sym_rows:
                    full_rows = pd.concat(sym_rows, ignore_index=True)
                    daily_by_feature[feature_name] = _daily_stability(full_rows)
                    magnitude_by_feature[feature_name] = _magnitude_buckets(full_rows)

        report["targets"][target_label] = {
            "groups": group_results,
            "daily_stability": daily_by_feature,
            "magnitude_buckets": magnitude_by_feature,
        }

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _log(f"STATUS: COMPLETED OUTPUT={OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
