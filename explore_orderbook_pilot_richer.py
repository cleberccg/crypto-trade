"""PILOT_EXPLORATORY_ONLY -- second, richer audit of the 7-day order-book pilot.

Uses ONLY data already collected by collect_orderbook_depth_bulk.py (no new
collection here). Reuses existing infra instead of reinventing statistics:
  - _entry_metrics / _passes_entry_gate / ENTRY_FORWARD_HORIZONS from
    strategy_discovery_cycle1.py (same episode/PF/t-stat/MFE/MAE convention
    used across every Discovery script in this repo).
  - add_microstructure_features from discover_microstructure_aggtrades.py to
    build the AGGTRADES_ONLY baseline features (imbalance_ratio,
    flow_absorption_signal) from our own aggTrades Parquet cache, unmodified.

Methodological correction vs the first pilot check: bookDepth is FUTURES UM
data, so the PRIMARY target here is a FUTURES price proxy derived from the
pilot bookDepth itself (mid = notional/depth average of the +-0.2% cumulative
bands, the narrowest available). SPOT (aggTrades) forward returns are only a
SECONDARY analysis, run after the futures-native result is characterized.

No look-ahead: every book/aggTrades feature at minute-bar T uses only data
with timestamp <= end of bar T (native bookDepth cadence ~30s, aggregated
with a right-closed/last-value resample). Entries execute at the OPEN of bar
T+1, exactly the convention already used by _entry_rows_for_config /
_entry_audit_once elsewhere in this repo. No snapshot is filled from the
future; minutes with no snapshot are simply NaN and dropped.

Does not touch BacktestEngine, RiskManager, PositionSizer,
ClassicDonchianBreakout, Paper Live or FINAL_HOLDOUT. Read-only over the
pilot Parquet files already on disk. No new data is downloaded.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from discover_microstructure_aggtrades import add_microstructure_features
from strategy_discovery_cycle1 import (
    ENTRY_FORWARD_HORIZONS,
    ENTRY_MIN_EFFECT_BPS,
    ENTRY_MIN_EPISODES,
    ENTRY_MIN_T_STAT,
    _entry_metrics,
    _passes_entry_gate,
)

BASE_DIR = Path(__file__).resolve().parent
OUT_JSON = BASE_DIR / "explore_orderbook_pilot_richer_latest.json"
OUT_MD = BASE_DIR / "explore_orderbook_pilot_richer_latest.md"

SYMBOLS = ("BTCUSDT", "ETHUSDT")
# Only days with BOTH bookDepth (futures) and aggTrades (spot) already
# collected and VALIDATED=YES (2026-08-24 aggTrades is NOT_AVAILABLE yet).
PILOT_DAYS = [f"2026-08-{d:02d}" for d in range(17, 24)]
NEAR_BANDS = (-1.0, -0.2, 0.2, 1.0)
FAR_BANDS = (-5.0, -4.0, -3.0, -2.0, 2.0, 3.0, 4.0, 5.0)
QUANTILE_HIGH = 0.90
QUANTILE_LOW = 0.10
MIN_DAYS_WITH_SIGNAL = 4  # >=4/7 pilot days must contribute episodes for "consistent across days"


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Loading pilot data already on disk (no new collection)
# ---------------------------------------------------------------------------

def _load_bookdepth(symbol: str) -> pd.DataFrame:
    frames = []
    for day in PILOT_DAYS:
        year, month, dd = day.split("-")
        path = BASE_DIR / "data" / "orderbook_depth" / symbol / year / month / f"{symbol}_{year}_{month}_{dd}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise RuntimeError(f"No pilot bookDepth parquet found for {symbol}")
    df = pd.concat(frames, ignore_index=True).sort_values(["timestamp", "percentage"])
    return df


def _load_aggtrades(symbol: str) -> pd.DataFrame:
    frames = []
    for day in PILOT_DAYS:
        year, month, dd = day.split("-")
        path = BASE_DIR / "data" / "aggtrades" / symbol / year / month / f"{symbol}_{year}_{month}_{dd}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise RuntimeError(f"No pilot aggTrades parquet found for {symbol}")
    df = pd.concat(frames, ignore_index=True).sort_values("T")
    return df


# ---------------------------------------------------------------------------
# FUTURES side: bands -> mid-price proxy, OHLC bars, and book features
# ---------------------------------------------------------------------------

def _pivot_bands(bd: pd.DataFrame) -> pd.DataFrame:
    wide = bd.pivot_table(index="timestamp", columns="percentage", values=["depth", "notional"])
    wide.columns = [f"{metric}_{pct:g}" for metric, pct in wide.columns]
    return wide.sort_index()


def _mid_price_proxy(wide: pd.DataFrame) -> pd.Series:
    """Average price of the narrowest cumulative bands (+-0.2%) as a mid proxy.
    Depth-weighted average of the two nearest-to-mid cumulative slices; this
    is a derivation from data already collected (notional/depth per band),
    not a new data source."""
    avg_neg = wide["notional_-0.2"] / wide["depth_-0.2"]
    avg_pos = wide["notional_0.2"] / wide["depth_0.2"]
    weight_neg = wide["depth_-0.2"]
    weight_pos = wide["depth_0.2"]
    mid = (avg_neg * weight_neg + avg_pos * weight_pos) / (weight_neg + weight_pos)
    return mid


def _col(metric: str, pct: float) -> str:
    return f"{metric}_{pct:g}"


def _futures_ohlc_1m(mid: pd.Series) -> pd.DataFrame:
    """Pseudo-OHLC per minute built only from the ~2 native 30s snapshots that
    fall inside each minute (no interpolation, no forward fill)."""
    g = mid.resample("1min", label="left", closed="left")
    ohlc = pd.DataFrame({"open": g.first(), "high": g.max(), "low": g.min(), "close": g.last(), "n_snapshots": g.count()})
    return ohlc.dropna(subset=["close"])


def _book_features_30s(wide: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(index=wide.index)
    bid_notional_cols = [c for c in wide.columns if c.startswith("notional_-")]
    ask_notional_cols = [c for c in wide.columns if c.startswith("notional_") and not c.startswith("notional_-")]
    df["bid_notional"] = wide[bid_notional_cols].sum(axis=1)
    df["ask_notional"] = wide[ask_notional_cols].sum(axis=1)

    # 1) BOOK_PRESSURE: notional imbalance weighted 1/|pct| (near bands count more).
    weighted_bid = sum(wide[_col("notional", p)] / abs(p) for p in (-5.0, -4.0, -3.0, -2.0, -1.0, -0.2))
    weighted_ask = sum(wide[_col("notional", p)] / abs(p) for p in (0.2, 1.0, 2.0, 3.0, 4.0, 5.0))
    df["book_pressure"] = (weighted_bid - weighted_ask) / (weighted_bid + weighted_ask)

    # 2) BOOK_PRESSURE_CHANGE / 3) BOOK_PRESSURE_ACCELERATION (snapshot-to-snapshot, causal).
    df["book_pressure_change"] = df["book_pressure"].diff()
    df["book_pressure_acceleration"] = df["book_pressure_change"].diff()

    # 4) LIQUIDITY_REMOVAL: abrupt one-sided depth drop (positive = bid drained more than ask).
    bid_drop = (-df["bid_notional"].pct_change()).clip(lower=0.0)
    ask_drop = (-df["ask_notional"].pct_change()).clip(lower=0.0)
    df["liquidity_removal"] = bid_drop - ask_drop

    # 5) LIQUIDITY_REFILL: pressure rebound conditioned on a removal spike in the prior snapshot.
    removal_hi = df["liquidity_removal"].abs().quantile(QUANTILE_HIGH)
    prior_removal_spike = df["liquidity_removal"].shift(1).abs() >= removal_hi
    df["liquidity_refill"] = df["book_pressure_change"].where(prior_removal_spike)

    # 6) NEAR_VS_FAR_LIQUIDITY: near-touch (0.2-1%) vs far (2-5%) concentration, per side, then side asymmetry.
    near_bid = wide[_col("notional", -0.2)] + wide[_col("notional", -1.0)]
    far_bid = wide[_col("notional", -4.0)] + wide[_col("notional", -5.0)]
    near_ask = wide[_col("notional", 0.2)] + wide[_col("notional", 1.0)]
    far_ask = wide[_col("notional", 4.0)] + wide[_col("notional", 5.0)]
    ratio_bid = near_bid / far_bid.replace(0.0, np.nan)
    ratio_ask = near_ask / far_ask.replace(0.0, np.nan)
    df["near_vs_far_liquidity"] = np.log(ratio_bid.replace(0, np.nan)) - np.log(ratio_ask.replace(0, np.nan))

    # 7) DEPTH_SLOPE_ASYMMETRY: slope of cumulative notional vs |pct| per side, bid slope - ask slope.
    bid_pcts = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    ask_pcts = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    bid_vals = np.column_stack([wide[_col("notional", -p)].to_numpy() for p in bid_pcts])
    ask_vals = np.column_stack([wide[_col("notional", p)].to_numpy() for p in ask_pcts])
    x = bid_pcts - bid_pcts.mean()
    denom = float((x**2).sum())
    slope_bid = ((bid_vals - bid_vals.mean(axis=1, keepdims=True)) @ x) / denom
    slope_ask = ((ask_vals - ask_vals.mean(axis=1, keepdims=True)) @ x) / denom
    df["depth_slope_asymmetry"] = slope_bid - slope_ask
    return df


def _resample_causal_1m(df_30s: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Value at minute bar T = last snapshot observed inside [T, T+1min)."""
    return df_30s[columns].resample("1min", label="left", closed="left").last()


# ---------------------------------------------------------------------------
# SPOT side: aggTrades -> 1-min OHLC + microstructure features (reused as-is)
# ---------------------------------------------------------------------------

def _aggtrades_minute_bars(agg: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(agg["T"], unit="ms", utc=True)
    price = agg["p"].astype(float)
    qty = agg["q"].astype(float)
    is_buy_aggressor = ~agg["m"].astype(bool)
    frame = pd.DataFrame({"ts": ts, "price": price, "qty": qty, "buy": is_buy_aggressor})
    frame["minute"] = frame["ts"].dt.floor("1min")
    grouped = frame.groupby("minute")
    bars = pd.DataFrame(
        {
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "volume": grouped["qty"].sum(),
            "buy_volume": frame[frame["buy"]].groupby("minute")["qty"].sum(),
            "sell_volume": frame[~frame["buy"]].groupby("minute")["qty"].sum(),
            "trade_count": grouped.size(),
        }
    )
    bars["buy_volume"] = bars["buy_volume"].fillna(0.0)
    bars["sell_volume"] = bars["sell_volume"].fillna(0.0)
    bars["avg_trade_size"] = bars["volume"] / bars["trade_count"].replace(0, np.nan)
    # max_aggressor_run per bar (used by add_microstructure_features's burst signal, not core to this audit).
    def _max_run(sub: pd.DataFrame) -> int:
        side = sub["buy"].to_numpy()
        if side.size == 0:
            return 0
        run = 1
        best = 1
        for i in range(1, side.size):
            run = run + 1 if side[i] == side[i - 1] else 1
            best = max(best, run)
        return best

    bars["max_aggressor_run"] = frame.groupby("minute").apply(_max_run)
    return bars.sort_index()


# ---------------------------------------------------------------------------
# Episode/entry-metrics evaluation (reusing _entry_metrics unmodified)
# ---------------------------------------------------------------------------

def _episode_rows(
    ohlc: pd.DataFrame,
    feature: pd.Series,
    direction: str,
    threshold: float,
) -> pd.DataFrame:
    """Same convention as _entry_rows_for_config: signal at bar close ->
    entry at OPEN of the next bar -> exit at close of entry+horizon-1."""
    aligned = ohlc.join(feature.rename("feature"), how="inner").dropna(subset=["feature"])
    if len(aligned) <= max(ENTRY_FORWARD_HORIZONS) + 5:
        return pd.DataFrame()
    entry_mask = (aligned["feature"] > threshold) if direction == "long_above" else (aligned["feature"] < threshold)
    entry_mask = entry_mask.fillna(False)
    starts = entry_mask & ~entry_mask.shift(fill_value=False)
    n = len(aligned)
    direction_sign = 1.0 if direction == "long_above" else -1.0
    records: list[dict[str, Any]] = []
    positions = np.flatnonzero(starts.to_numpy(bool))
    dates = aligned.index.date
    for signal_position in positions:
        entry_position = signal_position + 1
        if entry_position >= n:
            continue
        entry_price = float(aligned.iloc[entry_position]["open"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        for horizon in ENTRY_FORWARD_HORIZONS:
            exit_position = entry_position + horizon - 1
            if exit_position >= n:
                continue
            path = aligned.iloc[entry_position : exit_position + 1]
            forward_return = direction_sign * (float(path.iloc[-1]["close"]) / entry_price - 1.0)
            if direction_sign > 0:
                mfe = float(path["high"].max()) / entry_price - 1.0
                mae = float(path["low"].min()) / entry_price - 1.0
            else:
                mfe = -(float(path["low"].min()) / entry_price - 1.0)
                mae = -(float(path["high"].max()) / entry_price - 1.0)
            records.append(
                {
                    "horizon": horizon,
                    "signal_date": dates[signal_position].isoformat(),
                    "forward_return": forward_return,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
    return pd.DataFrame(records)


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


def _daily_stability(full_rows: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for day, chunk in full_rows.groupby("signal_date"):
        metrics = _entry_metrics(chunk["forward_return"], chunk["mfe"], chunk["mae"])
        out.append({"date": day, **metrics})
    return out


def _magnitude_buckets(full_rows: pd.DataFrame) -> dict[str, int]:
    abs_bps = (full_rows["forward_return"].abs() * 10_000.0)
    return {
        "<5bps": int((abs_bps < 5).sum()),
        "5-10bps": int(((abs_bps >= 5) & (abs_bps < 10)).sum()),
        "10-20bps": int(((abs_bps >= 10) & (abs_bps < 20)).sum()),
        ">20bps": int((abs_bps >= 20).sum()),
    }


def main() -> int:
    _log("STATUS: RUNNING CURRENT_STAGE=LOAD_PILOT_DATA (no new collection)")

    futures_ohlc: dict[str, pd.DataFrame] = {}
    book_1m: dict[str, pd.DataFrame] = {}
    spot_ohlc: dict[str, pd.DataFrame] = {}
    spot_features: dict[str, pd.DataFrame] = {}

    for symbol in SYMBOLS:
        bd = _load_bookdepth(symbol)
        wide = _pivot_bands(bd)
        mid = _mid_price_proxy(wide)
        futures_ohlc[symbol] = _futures_ohlc_1m(mid)
        book_feats_30s = _book_features_30s(wide)
        book_1m[symbol] = _resample_causal_1m(
            book_feats_30s,
            ["book_pressure", "book_pressure_change", "book_pressure_acceleration", "liquidity_removal", "liquidity_refill", "near_vs_far_liquidity", "depth_slope_asymmetry"],
        )
        agg = _load_aggtrades(symbol)
        bars = _aggtrades_minute_bars(agg)
        spot_ohlc[symbol] = bars[["open", "high", "low", "close"]]
        spot_features[symbol] = add_microstructure_features(bars)
        _log(f"STATUS: RUNNING CURRENT_STAGE=FEATURES_BUILT SYMBOL={symbol} FUTURES_BARS={len(futures_ohlc[symbol])} SPOT_BARS={len(spot_ohlc[symbol])}")

    # Combined features requiring both sides, aligned on the minute index.
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

    report: dict[str, Any] = {"pilot_days": PILOT_DAYS, "symbols": SYMBOLS, "targets": {}}

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
                # rebuild the winning episode rows for daily/magnitude breakdown
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
