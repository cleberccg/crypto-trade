from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd
from sqlalchemy import func

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.models import Candle
from research.services.market_regime_router_phase18 import (
    MarketRegimeRouter,
    TREND_REGIMES,
    VOL_REGIMES,
    _context_score,
    attach_trade_regimes,
    build_router_map,
    metrics_from_trades,
)
from strategies.factory import create_strategy

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "optimization" / "results" / "market_regime_router_phase18_5"


@dataclass(frozen=True)
class RollingConfig:
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    train_months: int
    test_months: int
    capital: float
    min_trades_per_regime: int
    baseline_strategy: str
    max_bars: int
    output_prefix: str
    phase18_report_file: str | None


@dataclass(frozen=True)
class Window:
    idx: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True)
class RegimeThresholds:
    trend_moderate_thr: float
    trend_strong_thr: float
    low_vol_thr: float
    high_vol_thr: float


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(dt: datetime, months: int) -> datetime:
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if pd.isna(out):
            return None
        return out
    except Exception:
        return None


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 6) if values else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FASE 18.5 - Rolling out-of-sample temporal validation for Market Regime Router",
    )
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    parser.add_argument("--timeframes", default="5m,15m,1h")
    parser.add_argument("--train-months", type=int, default=4)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--min-trades-per-regime", type=int, default=5)
    parser.add_argument("--baseline-strategy", default="ClassicEMACrossover")
    parser.add_argument("--max-bars", type=int, default=3500)
    parser.add_argument("--output-prefix", default="phase18_5_rolling_oos")
    parser.add_argument("--phase18-report-file", default=None)
    return parser.parse_args()


def load_phase18_reference(path: str | None) -> dict[str, Any]:
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"phase18 report not found: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    base = BASE_DIR / "optimization" / "results" / "market_regime_router"
    candidates = sorted(base.glob("phase18_market_regime_router_*.json"))
    candidates = [p for p in candidates if "official_summary" not in p.name]
    if not candidates:
        raise RuntimeError("No phase18_market_regime_router_*.json report found")
    latest = candidates[-1]
    return json.loads(latest.read_text(encoding="utf-8"))


def pick_candidate_platform_strategies(report: dict[str, Any]) -> list[str]:
    profiles = report.get("strategy_profiles", [])
    out: list[str] = []
    if isinstance(profiles, list):
        for row in profiles:
            if not isinstance(row, dict):
                continue
            name = str(row.get("platform_strategy_name") or "").strip()
            if name:
                out.append(name)
    out = sorted(set(out))
    if not out:
        raise RuntimeError("No platform strategies found in phase18 strategy_profiles")
    return out


def get_context_date_ranges(symbols: tuple[str, ...], timeframes: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with get_session() as session:
        for symbol in symbols:
            for timeframe in timeframes:
                lo, hi = (
                    session.query(func.min(Candle.open_time), func.max(Candle.open_time))
                    .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                    .one()
                )
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "min_open_time": lo,
                        "max_open_time": hi,
                        "has_data": bool(lo and hi),
                    }
                )
    return rows


def build_windows(global_min: datetime, global_max: datetime, train_months: int, test_months: int) -> list[Window]:
    anchor = month_start(global_min)
    if global_min > anchor:
        anchor = add_months(anchor, 1)

    windows: list[Window] = []
    test_start = add_months(anchor, train_months)
    idx = 1
    while True:
        test_end = add_months(test_start, test_months)
        if test_end > global_max:
            break
        train_start = add_months(test_start, -train_months)
        windows.append(
            Window(
                idx=idx,
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
            )
        )
        idx += 1
        test_start = add_months(test_start, 1)
    return windows


def load_candles(symbol: str, timeframe: str, start: datetime, end: datetime, max_bars: int) -> pd.DataFrame:
    with get_session() as session:
        candles = (
            session.query(Candle)
            .filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time < end,
            )
            .order_by(Candle.open_time)
            .all()
        )

    if not candles:
        return pd.DataFrame()

    frame = pd.DataFrame(
        [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )
    if max_bars > 0:
        frame = frame.tail(max_bars)
    return frame.copy()


def compute_features(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return candles.copy()
    df = candles.sort_index().copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")

    df["ema_fast"] = close.ewm(span=20, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=50, adjust=False).mean()
    df["trend_score"] = (df["ema_fast"] - df["ema_slow"]) / close.replace(0, pd.NA)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=8).mean()
    df["atr_pct"] = df["atr14"] / close.replace(0, pd.NA)
    return df


def derive_thresholds(train_features: pd.DataFrame) -> RegimeThresholds:
    abs_trend = pd.to_numeric(train_features["trend_score"], errors="coerce").abs()
    strong_thr = float(abs_trend.quantile(0.80)) if abs_trend.notna().any() else 0.0012
    moderate_thr = float(abs_trend.quantile(0.55)) if abs_trend.notna().any() else 0.0006
    strong_thr = max(strong_thr, 0.0012)
    moderate_thr = max(moderate_thr, 0.0006)

    atr_pct = pd.to_numeric(train_features["atr_pct"], errors="coerce")
    low_vol_thr = float(atr_pct.quantile(0.30)) if atr_pct.notna().any() else 0.0
    high_vol_thr = float(atr_pct.quantile(0.70)) if atr_pct.notna().any() else 0.0

    return RegimeThresholds(
        trend_moderate_thr=moderate_thr,
        trend_strong_thr=strong_thr,
        low_vol_thr=low_vol_thr,
        high_vol_thr=high_vol_thr,
    )


def apply_regime_labels(features: pd.DataFrame, thresholds: RegimeThresholds) -> pd.DataFrame:
    if features.empty:
        return features.copy()
    out = features.copy()

    out["trend_bucket"] = "sideways"
    out.loc[out["trend_score"] >= thresholds.trend_moderate_thr, "trend_bucket"] = "bullish"
    out.loc[out["trend_score"] <= -thresholds.trend_moderate_thr, "trend_bucket"] = "bearish"

    out["vol_regime"] = "normal_volatility"
    out.loc[out["atr_pct"] <= thresholds.low_vol_thr, "vol_regime"] = "low_volatility"
    out.loc[out["atr_pct"] >= thresholds.high_vol_thr, "vol_regime"] = "high_volatility"
    out["regime_key"] = out["trend_bucket"].astype(str) + "|" + out["vol_regime"].astype(str)
    return out


def run_backtest(platform_strategy: str, symbol: str, timeframe: str, candles: pd.DataFrame, capital: float) -> dict[str, Any]:
    if candles.empty:
        return {"trades": pd.DataFrame(), "metrics": metrics_from_trades(pd.DataFrame(), capital)}

    strategy = create_strategy(platform_strategy)
    strategy.initialize()
    result = BacktestEngine(strategy, config=BacktestConfig(initial_capital=capital)).run(
        candles,
        symbol=symbol,
        timeframe=timeframe,
    )

    metrics = result.metrics
    return {
        "trades": pd.DataFrame(result.trades),
        "metrics": {
            "number_of_trades": int(metrics.total_trades),
            "profit_factor": float(metrics.profit_factor),
            "sharpe": float(metrics.sharpe_ratio),
            "expectancy": float(metrics.expectancy),
            "drawdown_pct": float(metrics.max_drawdown_pct),
            "return_pct": float(metrics.return_pct),
            "net_profit": float(metrics.net_profit),
            "win_rate": float(metrics.win_rate),
        },
    }


def compose_router_trades(
    symbol: str,
    timeframe: str,
    strategy_trade_books: dict[str, pd.DataFrame],
    router: MarketRegimeRouter,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for platform_name, trades in strategy_trade_books.items():
        if trades.empty:
            continue
        trades = trades.copy()
        trades["platform_strategy_name"] = platform_name
        rows.extend(trades.to_dict("records"))

    if not rows:
        return pd.DataFrame()

    merged = pd.DataFrame(rows)
    merged["entry_time"] = pd.to_datetime(merged["entry_time"], utc=True)
    merged["exit_time"] = pd.to_datetime(merged["exit_time"], utc=True)

    selected: list[dict[str, Any]] = []
    for row in merged.sort_values("entry_time").to_dict("records"):
        trend_bucket = str(row.get("trend_bucket") or "sideways")
        vol_regime = str(row.get("vol_regime") or "normal_volatility")
        rec = router.recommend(symbol, timeframe, trend_bucket, vol_regime)
        if str(row.get("platform_strategy_name") or "") != str(rec.get("platform_strategy") or ""):
            continue
        row["router_selected_strategy"] = rec.get("strategy")
        row["router_selection_source"] = rec.get("source")
        selected.append(row)

    if not selected:
        return pd.DataFrame()

    accepted: list[dict[str, Any]] = []
    active_until = None
    for row in sorted(selected, key=lambda x: pd.Timestamp(x["entry_time"])):
        entry_time = pd.Timestamp(row["entry_time"])
        exit_time = pd.Timestamp(row["exit_time"])
        if active_until is not None and entry_time < active_until:
            continue
        accepted.append(row)
        active_until = exit_time

    return pd.DataFrame(accepted)


def is_router_better(row: dict[str, Any]) -> bool:
    pf_gain = float(row.get("router_profit_factor") or 0.0) - float(row.get("single_profit_factor") or 0.0)
    sharpe_gain = float(row.get("router_sharpe") or 0.0) - float(row.get("single_sharpe") or 0.0)
    ret_gain = float(row.get("router_return_pct") or 0.0) - float(row.get("single_return_pct") or 0.0)
    dd_gain = float(row.get("router_drawdown_pct") or 0.0) - float(row.get("single_drawdown_pct") or 0.0)
    score = 0
    if pf_gain > 0:
        score += 1
    if sharpe_gain > 0:
        score += 1
    if ret_gain > 0:
        score += 1
    if dd_gain <= 0:
        score += 1
    return score >= 3


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def evaluate_classification(summary: dict[str, Any]) -> str:
    win_pct = float(summary.get("router_win_pct") or 0.0)
    dd_gain = _safe_float(summary.get("avg_drawdown_delta"))
    diversified = bool(summary.get("broad_asset_timeframe_support"))

    if win_pct > 0.5 and (dd_gain is not None and dd_gain < 0) and diversified:
        return "CONFIRMADA"
    if win_pct >= 0.35:
        return "PARCIALMENTE CONFIRMADA"
    return "REFUTADA"


def main() -> None:
    args = parse_args()
    symbols = tuple([s.strip() for s in str(args.symbols).split(",") if s.strip()])
    timeframes = tuple([t.strip() for t in str(args.timeframes).split(",") if t.strip()])

    cfg = RollingConfig(
        symbols=symbols,
        timeframes=timeframes,
        train_months=max(1, int(args.train_months)),
        test_months=max(1, int(args.test_months)),
        capital=max(100.0, float(args.capital)),
        min_trades_per_regime=max(1, int(args.min_trades_per_regime)),
        baseline_strategy=str(args.baseline_strategy),
        max_bars=int(args.max_bars),
        output_prefix=str(args.output_prefix),
        phase18_report_file=str(args.phase18_report_file) if args.phase18_report_file else None,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    windows_dir = OUT_DIR / "windows"
    windows_dir.mkdir(parents=True, exist_ok=True)

    phase18_report = load_phase18_reference(cfg.phase18_report_file)
    candidate_platforms = pick_candidate_platform_strategies(phase18_report)
    if cfg.baseline_strategy not in candidate_platforms:
        candidate_platforms = sorted(set(candidate_platforms + [cfg.baseline_strategy]))

    coverage = get_context_date_ranges(cfg.symbols, cfg.timeframes)
    missing = [r for r in coverage if not r["has_data"]]

    valid_contexts = [r for r in coverage if r["has_data"]]
    global_min = max([r["min_open_time"] for r in valid_contexts]) if valid_contexts else None
    global_max = min([r["max_open_time"] for r in valid_contexts]) if valid_contexts else None

    windows: list[Window] = []
    blocked_reason = None
    if missing:
        blocked_reason = "Missing candle data in one or more required contexts"
    elif global_min is None or global_max is None:
        blocked_reason = "No candle data available for required contexts"
    else:
        windows = build_windows(global_min, global_max, cfg.train_months, cfg.test_months)
        if not windows:
            blocked_reason = (
                "Insufficient temporal coverage for strict full-matrix rolling windows "
                f"(need {cfg.train_months} training months + {cfg.test_months} testing month)."
            )

    window_rows: list[dict[str, Any]] = []
    consolidated_rows: list[dict[str, Any]] = []
    robustness_rows: list[dict[str, Any]] = []
    metrics_rows: list[dict[str, Any]] = []
    router_decisions_rows: list[dict[str, Any]] = []
    regime_dist_rows: list[dict[str, Any]] = []
    utilization_rows: list[dict[str, Any]] = []

    if not blocked_reason:
        for win in windows:
            per_context_train: dict[tuple[str, str], dict[str, Any]] = {}

            for symbol in cfg.symbols:
                for timeframe in cfg.timeframes:
                    train_df = load_candles(symbol, timeframe, win.train_start, win.train_end, cfg.max_bars)
                    test_df = load_candles(symbol, timeframe, win.test_start, win.test_end, cfg.max_bars)
                    if train_df.empty or test_df.empty:
                        continue

                    train_feat = compute_features(train_df)
                    thresholds = derive_thresholds(train_feat)
                    train_regimes = apply_regime_labels(train_feat, thresholds)
                    test_feat = compute_features(test_df)
                    test_regimes = apply_regime_labels(test_feat, thresholds)

                    per_context_train[(symbol, timeframe)] = {
                        "train_df": train_df,
                        "test_df": test_df,
                        "train_regimes": train_regimes,
                        "test_regimes": test_regimes,
                        "thresholds": thresholds,
                    }

            if not per_context_train:
                continue

            training_rows: list[dict[str, Any]] = []
            for platform_strategy in candidate_platforms:
                for (symbol, timeframe), ctx in per_context_train.items():
                    train_bt = run_backtest(platform_strategy, symbol, timeframe, ctx["train_df"], cfg.capital)
                    train_trades = train_bt["trades"]
                    trade_regimes = attach_trade_regimes(train_trades, ctx["train_regimes"]) if not train_trades.empty else pd.DataFrame()

                    for trend_bucket in TREND_REGIMES:
                        for vol_regime in VOL_REGIMES:
                            if trade_regimes.empty:
                                subset = trade_regimes
                            else:
                                subset = trade_regimes[
                                    (trade_regimes["trend_bucket"] == trend_bucket)
                                    & (trade_regimes["vol_regime"] == vol_regime)
                                ]
                            metrics = metrics_from_trades(subset, cfg.capital)
                            row = {
                                "window_id": win.idx,
                                "window_label": (
                                    f"{win.train_start:%Y-%m}->{(win.train_end - pd.Timedelta(days=1)).strftime('%Y-%m')}"
                                    f" / {win.test_start:%Y-%m}"
                                ),
                                "strategy": platform_strategy,
                                "platform_strategy_name": platform_strategy,
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "trend_bucket": trend_bucket,
                                "vol_regime": vol_regime,
                                "regime": f"{trend_bucket}|{vol_regime}",
                                **metrics,
                            }
                            row["context_score"] = _context_score(row)
                            if int(row.get("number_of_trades") or 0) >= cfg.min_trades_per_regime:
                                training_rows.append(row)

            if not training_rows:
                continue

            mapping = build_router_map(training_rows)
            router = MarketRegimeRouter(
                mapping=mapping,
                default_strategy=cfg.baseline_strategy,
                default_platform_strategy=cfg.baseline_strategy,
            )

            single_context_rows: list[dict[str, Any]] = []
            router_context_rows: list[dict[str, Any]] = []
            agg_single_trades: list[dict[str, Any]] = []
            agg_router_trades: list[dict[str, Any]] = []

            for (symbol, timeframe), ctx in per_context_train.items():
                test_df = ctx["test_df"]
                test_regimes = ctx["test_regimes"]

                baseline_bt = run_backtest(cfg.baseline_strategy, symbol, timeframe, test_df, cfg.capital)
                single_metrics = baseline_bt["metrics"]
                single_context_rows.append(
                    {
                        "window_id": win.idx,
                        "window_label": (
                            f"{win.train_start:%Y-%m}->{(win.train_end - pd.Timedelta(days=1)).strftime('%Y-%m')}"
                            f" / {win.test_start:%Y-%m}"
                        ),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "scenario": "baseline",
                        "strategy": cfg.baseline_strategy,
                        **single_metrics,
                    }
                )

                baseline_trades = baseline_bt["trades"].copy()
                if not baseline_trades.empty:
                    baseline_trades["symbol"] = symbol
                    baseline_trades["timeframe"] = timeframe
                    baseline_trades["window_id"] = win.idx
                    agg_single_trades.extend(baseline_trades.to_dict("records"))

                strategy_trade_books: dict[str, pd.DataFrame] = {}
                for platform_strategy in candidate_platforms:
                    bt = run_backtest(platform_strategy, symbol, timeframe, test_df, cfg.capital)
                    trades_df = bt["trades"]
                    if trades_df.empty:
                        strategy_trade_books[platform_strategy] = trades_df
                        continue
                    trades_df = attach_trade_regimes(trades_df, test_regimes)
                    trades_df["platform_strategy_name"] = platform_strategy
                    strategy_trade_books[platform_strategy] = trades_df

                routed = compose_router_trades(symbol, timeframe, strategy_trade_books, router)
                routed_metrics = metrics_from_trades(routed, cfg.capital)
                router_context_rows.append(
                    {
                        "window_id": win.idx,
                        "window_label": (
                            f"{win.train_start:%Y-%m}->{(win.train_end - pd.Timedelta(days=1)).strftime('%Y-%m')}"
                            f" / {win.test_start:%Y-%m}"
                        ),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "scenario": "router",
                        "strategy": "dynamic",
                        **routed_metrics,
                    }
                )

                if not routed.empty:
                    routed["symbol"] = symbol
                    routed["timeframe"] = timeframe
                    routed["window_id"] = win.idx
                    agg_router_trades.extend(routed.to_dict("records"))

                # Router frozen decisions on every test bar.
                decisions = test_regimes[["trend_bucket", "vol_regime"]].copy()
                decisions = decisions.reset_index().rename(columns={decisions.index.name or "index": "timestamp"})
                decisions["symbol"] = symbol
                decisions["timeframe"] = timeframe
                decisions["window_id"] = win.idx
                labels = []
                sources = []
                for rec in decisions.to_dict("records"):
                    recommendation = router.recommend(symbol, timeframe, str(rec["trend_bucket"]), str(rec["vol_regime"]))
                    labels.append(str(recommendation.get("platform_strategy") or cfg.baseline_strategy))
                    sources.append(str(recommendation.get("source") or "default"))
                decisions["recommended_platform_strategy"] = labels
                decisions["selection_source"] = sources
                decisions = decisions.sort_values("timestamp")
                prev = decisions["recommended_platform_strategy"].shift(1)
                decisions["strategy_changed"] = (decisions["recommended_platform_strategy"] != prev).fillna(False)
                router_decisions_rows.extend(decisions.to_dict("records"))

                reg_count = (
                    decisions.groupby(["trend_bucket", "vol_regime"], as_index=False)
                    .size()
                    .rename(columns={"size": "bars"})
                )
                total_bars = float(reg_count["bars"].sum()) if len(reg_count) else 0.0
                for rec in reg_count.to_dict("records"):
                    rec["window_id"] = win.idx
                    rec["symbol"] = symbol
                    rec["timeframe"] = timeframe
                    rec["pct"] = (float(rec["bars"]) / total_bars) if total_bars > 0 else 0.0
                    regime_dist_rows.append(rec)

                util = (
                    decisions.groupby("recommended_platform_strategy", as_index=False)
                    .size()
                    .rename(columns={"size": "uses"})
                )
                util_total = float(util["uses"].sum()) if len(util) else 0.0
                for rec in util.to_dict("records"):
                    rec["window_id"] = win.idx
                    rec["symbol"] = symbol
                    rec["timeframe"] = timeframe
                    rec["pct"] = (float(rec["uses"]) / util_total) if util_total > 0 else 0.0
                    utilization_rows.append(rec)

            single_agg = metrics_from_trades(pd.DataFrame(agg_single_trades), cfg.capital)
            router_agg = metrics_from_trades(pd.DataFrame(agg_router_trades), cfg.capital)

            by_context_router = {
                (str(r.get("symbol")), str(r.get("timeframe"))): r
                for r in router_context_rows
            }
            window_robust_rows: list[dict[str, Any]] = []
            for base_row in single_context_rows:
                key = (str(base_row.get("symbol")), str(base_row.get("timeframe")))
                rr = by_context_router.get(key, {})
                row = {
                    "window_id": win.idx,
                    "window_label": base_row.get("window_label"),
                    "symbol": key[0],
                    "timeframe": key[1],
                    "single_profit_factor": _safe_float(base_row.get("profit_factor")),
                    "router_profit_factor": _safe_float(rr.get("profit_factor")),
                    "single_sharpe": _safe_float(base_row.get("sharpe")),
                    "router_sharpe": _safe_float(rr.get("sharpe")),
                    "single_expectancy": _safe_float(base_row.get("expectancy")),
                    "router_expectancy": _safe_float(rr.get("expectancy")),
                    "single_drawdown_pct": _safe_float(base_row.get("drawdown_pct")),
                    "router_drawdown_pct": _safe_float(rr.get("drawdown_pct")),
                    "single_return_pct": _safe_float(base_row.get("return_pct")),
                    "router_return_pct": _safe_float(rr.get("return_pct")),
                }
                row["router_better"] = is_router_better(row)
                window_robust_rows.append(row)
                consolidated_rows.append(row)

            router_win_count = int(sum(1 for r in window_robust_rows if r["router_better"]))
            base_win_count = int(len(window_robust_rows) - router_win_count)
            win_pct = (router_win_count / len(window_robust_rows)) if window_robust_rows else 0.0

            window_row = {
                "window_id": win.idx,
                "train_start": win.train_start.isoformat(),
                "train_end": win.train_end.isoformat(),
                "test_start": win.test_start.isoformat(),
                "test_end": win.test_end.isoformat(),
                "window_label": (
                    f"{win.train_start:%Y-%m}->{(win.train_end - pd.Timedelta(days=1)).strftime('%Y-%m')}"
                    f" / {win.test_start:%Y-%m}"
                ),
                "contexts_evaluated": len(window_robust_rows),
                "router_wins": router_win_count,
                "baseline_wins": base_win_count,
                "draws": 0,
                "router_win_pct": win_pct,
                "baseline_win_pct": (base_win_count / len(window_robust_rows)) if window_robust_rows else 0.0,
                "single_profit_factor": single_agg.get("profit_factor"),
                "router_profit_factor": router_agg.get("profit_factor"),
                "single_sharpe": single_agg.get("sharpe"),
                "router_sharpe": router_agg.get("sharpe"),
                "single_expectancy": single_agg.get("expectancy"),
                "router_expectancy": router_agg.get("expectancy"),
                "single_drawdown_pct": single_agg.get("drawdown_pct"),
                "router_drawdown_pct": router_agg.get("drawdown_pct"),
                "single_return_pct": single_agg.get("return_pct"),
                "router_return_pct": router_agg.get("return_pct"),
                "single_net_profit": single_agg.get("net_profit"),
                "router_net_profit": router_agg.get("net_profit"),
                "single_win_rate": single_agg.get("win_rate"),
                "router_win_rate": router_agg.get("win_rate"),
                "single_number_of_trades": single_agg.get("number_of_trades"),
                "router_number_of_trades": router_agg.get("number_of_trades"),
                "profit_factor_delta": _safe_float(router_agg.get("profit_factor")) - _safe_float(single_agg.get("profit_factor")) if _safe_float(router_agg.get("profit_factor")) is not None and _safe_float(single_agg.get("profit_factor")) is not None else None,
                "sharpe_delta": _safe_float(router_agg.get("sharpe")) - _safe_float(single_agg.get("sharpe")) if _safe_float(router_agg.get("sharpe")) is not None and _safe_float(single_agg.get("sharpe")) is not None else None,
                "expectancy_delta": _safe_float(router_agg.get("expectancy")) - _safe_float(single_agg.get("expectancy")) if _safe_float(router_agg.get("expectancy")) is not None and _safe_float(single_agg.get("expectancy")) is not None else None,
                "drawdown_delta": _safe_float(router_agg.get("drawdown_pct")) - _safe_float(single_agg.get("drawdown_pct")) if _safe_float(router_agg.get("drawdown_pct")) is not None and _safe_float(single_agg.get("drawdown_pct")) is not None else None,
                "return_delta": _safe_float(router_agg.get("return_pct")) - _safe_float(single_agg.get("return_pct")) if _safe_float(router_agg.get("return_pct")) is not None and _safe_float(single_agg.get("return_pct")) is not None else None,
            }
            window_rows.append(window_row)

            write_csv(windows_dir / f"{cfg.output_prefix}_window_{win.idx:03d}.csv", window_robust_rows)
            robustness_rows.extend(window_robust_rows)

    # Consolidations
    total_windows = len(window_rows)
    router_wins_total = int(sum(int(r.get("router_wins") or 0) for r in window_rows))
    baseline_wins_total = int(sum(int(r.get("baseline_wins") or 0) for r in window_rows))
    draws_total = int(sum(int(r.get("draws") or 0) for r in window_rows))
    contexts_total = int(sum(int(r.get("contexts_evaluated") or 0) for r in window_rows))

    router_win_pct = (router_wins_total / contexts_total) if contexts_total else 0.0
    baseline_win_pct = (baseline_wins_total / contexts_total) if contexts_total else 0.0
    draw_pct = (draws_total / contexts_total) if contexts_total else 0.0

    avg_pf_gain = _avg([float(r["profit_factor_delta"]) for r in window_rows if r.get("profit_factor_delta") is not None])
    avg_sharpe_gain = _avg([float(r["sharpe_delta"]) for r in window_rows if r.get("sharpe_delta") is not None])
    avg_expectancy_gain = _avg([float(r["expectancy_delta"]) for r in window_rows if r.get("expectancy_delta") is not None])
    avg_dd_reduction = _avg([float(r["drawdown_delta"]) for r in window_rows if r.get("drawdown_delta") is not None])
    avg_return_gain = _avg([float(r["return_delta"]) for r in window_rows if r.get("return_delta") is not None])

    # Generalization by asset/timeframe
    by_asset: list[dict[str, Any]] = []
    by_timeframe: list[dict[str, Any]] = []

    if consolidated_rows:
        robust_df = pd.DataFrame(consolidated_rows)
        for asset, g in robust_df.groupby("symbol"):
            total = len(g)
            wins = int(g["router_better"].sum())
            by_asset.append(
                {
                    "scope": "asset",
                    "key": asset,
                    "router_wins": wins,
                    "baseline_wins": total - wins,
                    "win_pct": (wins / total) if total else 0.0,
                    "total": total,
                }
            )
        for tf, g in robust_df.groupby("timeframe"):
            total = len(g)
            wins = int(g["router_better"].sum())
            by_timeframe.append(
                {
                    "scope": "timeframe",
                    "key": tf,
                    "router_wins": wins,
                    "baseline_wins": total - wins,
                    "win_pct": (wins / total) if total else 0.0,
                    "total": total,
                }
            )

    util_summary: list[dict[str, Any]] = []
    if utilization_rows:
        util_df = pd.DataFrame(utilization_rows)
        grouped = (
            util_df.groupby(["window_id", "recommended_platform_strategy"], as_index=False)["uses"]
            .sum()
            .sort_values(["window_id", "uses"], ascending=[True, False])
        )
        for window_id, g in grouped.groupby("window_id"):
            total = float(g["uses"].sum()) if len(g) else 0.0
            top_pct = (float(g.iloc[0]["uses"]) / total) if total > 0 else 0.0
            util_summary.append(
                {
                    "window_id": int(window_id),
                    "strategies_used": int(len(g)),
                    "top1_concentration": top_pct,
                }
            )

    switch_summary: list[dict[str, Any]] = []
    if router_decisions_rows:
        dec_df = pd.DataFrame(router_decisions_rows)
        for window_id, g in dec_df.groupby("window_id"):
            changes = int(g["strategy_changed"].astype(bool).sum())
            bars = int(len(g))
            switch_summary.append(
                {
                    "window_id": int(window_id),
                    "switch_count": changes,
                    "switch_frequency": (changes / bars) if bars else 0.0,
                }
            )

    broad_asset_timeframe_support = all([r.get("win_pct", 0.0) >= 0.5 for r in by_asset + by_timeframe]) if (by_asset or by_timeframe) else False

    summary = {
        "status": "blocked" if blocked_reason else "ok",
        "blocked_reason": blocked_reason,
        "number_of_windows": total_windows,
        "contexts_total": contexts_total,
        "router_wins": router_wins_total,
        "baseline_wins": baseline_wins_total,
        "draws": draws_total,
        "router_win_pct": router_win_pct,
        "baseline_win_pct": baseline_win_pct,
        "draw_pct": draw_pct,
        "avg_profit_factor_gain": avg_pf_gain,
        "avg_sharpe_gain": avg_sharpe_gain,
        "avg_expectancy_gain": avg_expectancy_gain,
        "avg_drawdown_delta": avg_dd_reduction,
        "avg_return_gain": avg_return_gain,
        "broad_asset_timeframe_support": broad_asset_timeframe_support,
        "asset_support": by_asset,
        "timeframe_support": by_timeframe,
        "switch_summary": switch_summary,
        "utilization_summary": util_summary,
    }

    classification = "REFUTADA" if blocked_reason else evaluate_classification(summary)

    metrics_rows.extend(
        [
            {"metric": "Profit Factor", "avg_baseline": _avg([float(r["single_profit_factor"]) for r in window_rows if r.get("single_profit_factor") is not None]), "avg_router": _avg([float(r["router_profit_factor"]) for r in window_rows if r.get("router_profit_factor") is not None]), "avg_delta": avg_pf_gain},
            {"metric": "Sharpe", "avg_baseline": _avg([float(r["single_sharpe"]) for r in window_rows if r.get("single_sharpe") is not None]), "avg_router": _avg([float(r["router_sharpe"]) for r in window_rows if r.get("router_sharpe") is not None]), "avg_delta": avg_sharpe_gain},
            {"metric": "Expectancy", "avg_baseline": _avg([float(r["single_expectancy"]) for r in window_rows if r.get("single_expectancy") is not None]), "avg_router": _avg([float(r["router_expectancy"]) for r in window_rows if r.get("router_expectancy") is not None]), "avg_delta": avg_expectancy_gain},
            {"metric": "Drawdown", "avg_baseline": _avg([float(r["single_drawdown_pct"]) for r in window_rows if r.get("single_drawdown_pct") is not None]), "avg_router": _avg([float(r["router_drawdown_pct"]) for r in window_rows if r.get("router_drawdown_pct") is not None]), "avg_delta": avg_dd_reduction},
            {"metric": "Retorno", "avg_baseline": _avg([float(r["single_return_pct"]) for r in window_rows if r.get("single_return_pct") is not None]), "avg_router": _avg([float(r["router_return_pct"]) for r in window_rows if r.get("router_return_pct") is not None]), "avg_delta": avg_return_gain},
        ]
    )

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "phase": "FASE 18.5 - Rolling Out-of-Sample Temporal",
        "objective": "Validate if phase18 gains persist in unseen future periods",
        "config": {
            "symbols": list(cfg.symbols),
            "timeframes": list(cfg.timeframes),
            "train_months": cfg.train_months,
            "test_months": cfg.test_months,
            "capital": cfg.capital,
            "baseline_strategy": cfg.baseline_strategy,
            "min_trades_per_regime": cfg.min_trades_per_regime,
            "max_bars": cfg.max_bars,
            "router_frozen_during_test": True,
            "no_future_data_in_training": True,
            "test_regime_thresholds_frozen_from_training": True,
        },
        "coverage": coverage,
        "summary": summary,
        "classification": classification,
        "windows": window_rows,
        "metrics": metrics_rows,
        "robustness_temporal": by_asset + by_timeframe,
    }

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"{cfg.output_prefix}_{stamp}.json"
    md_path = OUT_DIR / f"{cfg.output_prefix}_{stamp}.md"
    windows_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_windows.csv"
    consolidated_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_consolidated.csv"
    robustness_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_temporal_robustness.csv"
    metrics_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_metrics.csv"
    decisions_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_router_decisions.csv"
    regime_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_regime_distribution.csv"
    util_csv = OUT_DIR / f"{cfg.output_prefix}_{stamp}_strategy_utilization.csv"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines: list[str] = []
    lines.append("# FASE 18.5 - Validacao Rolling Out-of-Sample Temporal")
    lines.append("")
    lines.append("## Hipotese")
    lines.append("O ganho observado na FASE 18 permanece em periodos nunca utilizados no treinamento.")
    lines.append("")
    lines.append("## Resultado")
    lines.append(classification)
    lines.append("")
    if blocked_reason:
        lines.append("## Bloqueio Cientifico")
        lines.append(f"- {blocked_reason}")
        lines.append("- Sem janelas validas para avaliacao strict full-matrix.")
        lines.append("")

    lines.append("## Persistencia do ganho")
    if window_rows:
        for row in window_rows:
            label = row.get("window_label")
            if int(row.get("router_wins") or 0) > int(row.get("baseline_wins") or 0):
                verdict = "Router venceu"
            elif int(row.get("router_wins") or 0) < int(row.get("baseline_wins") or 0):
                verdict = "Baseline venceu"
            else:
                verdict = "Empate"
            lines.append(f"- {label}: {verdict}")
    else:
        lines.append("- Nenhuma janela avaliada.")

    lines.append("")
    lines.append("## Frequencias")
    lines.append(f"- Router venceu: {router_wins_total}/{contexts_total} ({router_win_pct:.2%})" if contexts_total else "- Router venceu: n/a")
    lines.append(f"- Baseline venceu: {baseline_wins_total}/{contexts_total} ({baseline_win_pct:.2%})" if contexts_total else "- Baseline venceu: n/a")
    lines.append(f"- Empates: {draws_total}/{contexts_total} ({draw_pct:.2%})" if contexts_total else "- Empates: n/a")

    lines.append("")
    lines.append("## Ganho medio")
    lines.append(f"- Profit Factor: {avg_pf_gain}" )
    lines.append(f"- Sharpe: {avg_sharpe_gain}")
    lines.append(f"- Expectancy: {avg_expectancy_gain}")
    lines.append(f"- Drawdown (delta): {avg_dd_reduction}")
    lines.append(f"- Retorno: {avg_return_gain}")

    lines.append("")
    lines.append("## Generalizacao")
    if by_asset:
        lines.append("- Por ativo:")
        for row in by_asset:
            lines.append(f"  - {row['key']}: {row['router_wins']}/{row['total']} ({row['win_pct']:.2%})")
    if by_timeframe:
        lines.append("- Por timeframe:")
        for row in by_timeframe:
            lines.append(f"  - {row['key']}: {row['router_wins']}/{row['total']} ({row['win_pct']:.2%})")
    if not by_asset and not by_timeframe:
        lines.append("- Sem dados para analise de generalizacao.")

    lines.append("")
    lines.append("## Resumo executivo")
    lines.append("Hipotese:")
    lines.append("O ganho observado na FASE 18 permanece em periodos nunca utilizados no treinamento.")
    lines.append("")
    lines.append("Resultado:")
    lines.append(classification)
    lines.append("")
    lines.append("Motivos:")
    if blocked_reason:
        lines.append(f"- {blocked_reason}")
        lines.append("- Nao houve janelas suficientes para treino 4 meses + teste 1 mes com todas as combinacoes exigidas.")
        lines.append("- Conclusao cientifica fica limitada por cobertura temporal insuficiente.")
    else:
        lines.append(f"- Router venceu {router_wins_total} de {contexts_total} contextos em janelas OOS.")
        lines.append(f"- Delta medio de drawdown: {avg_dd_reduction}.")
        lines.append(f"- Suporte em ativos/timeframes: {'SIM' if broad_asset_timeframe_support else 'NAO'}.")
    lines.append("")
    lines.append("Numero de janelas avaliadas:")
    lines.append(str(total_windows))
    lines.append("")
    lines.append("Router venceu:")
    lines.append(str(router_wins_total))
    lines.append("")
    lines.append("Baseline venceu:")
    lines.append(str(baseline_wins_total))
    lines.append("")
    lines.append("Empates:")
    lines.append(str(draws_total))
    lines.append("")
    lines.append("Recomendacao cientifica:")
    if blocked_reason:
        lines.append("Retornar para pesquisa de estrategias individuais.")
    elif classification == "CONFIRMADA":
        lines.append("Prosseguir para FASE 19.")
    else:
        lines.append("Retornar para pesquisa de estrategias individuais.")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    write_csv(windows_csv, window_rows)
    write_csv(consolidated_csv, consolidated_rows)
    write_csv(robustness_csv, by_asset + by_timeframe)
    write_csv(metrics_csv, metrics_rows)
    write_csv(decisions_csv, router_decisions_rows)
    write_csv(regime_csv, regime_dist_rows)
    write_csv(util_csv, utilization_rows)

    print(str(json_path))
    print(str(md_path))
    print(str(windows_csv))
    print(str(consolidated_csv))
    print(str(robustness_csv))
    print(str(metrics_csv))
    print(str(decisions_csv))
    print(str(regime_csv))
    print(str(util_csv))


if __name__ == "__main__":
    main()
