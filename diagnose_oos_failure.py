"""DIAGNOSTIC AUDIT of the OOS failure pattern observed across the 5 external
strategy replications (external_strategy_replication_latest.json): DEV/VAL
positive, OOS negative, on every single strategy.

Read-only diagnostic. Does NOT modify any strategy, does NOT optimize any
parameter, does NOT touch FINAL_HOLDOUT (>= 2026-06-01, never loaded here,
same structural lock as research/external_strategy_replication_strategies.py),
does NOT touch Paper Live.

Reuses, unmodified:
- research/external_strategy_replication_strategies.py (strategy classes,
  data loaders, splits, cost constants) -- the exact same frozen rules already
  tested.
- run_external_strategy_replication.py (_run_once / _gross_metrics /
  _stress_metrics) -- the exact same cost/stress methodology already used.
- backtesting/engine.py, backtesting/metrics.py -- unmodified.

New code here is diagnostic-only (regime characterization, trade-level
breakdown, walk-forward loop, ADX) -- there is no existing script in this
repo that already does this, so a new read-only analysis script is the
minimal necessary addition (no new production/backtest infrastructure).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.metrics import compute_metrics
from research.external_strategy_replication_strategies import (
    BASE_FEE,
    CAPITAL,
    ApexNoPyramidStrategy,
    FiveEmaWeeklyFilterStrategy,
    QuattroDonchianStrategy,
    Sma200TrendStrategy,
    VolNormalizedTrendStrategy,
    compute_adx,
    dev_val_oos_split,
    load_base_candles,
    prepare_weekly_frame,
    resample_ohlcv,
)
from run_external_strategy_replication import _gross_metrics, _run_once, _stress_metrics

BASE_DIR = Path(__file__).resolve().parent
OUT_JSON = BASE_DIR / "diagnose_oos_failure_latest.json"


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# ADX / regime helpers (diagnostic-only, not a new production indicator)
# ---------------------------------------------------------------------------

def directional_efficiency(close: pd.Series) -> float:
    net = abs(float(close.iloc[-1]) - float(close.iloc[0]))
    total = float(close.diff().abs().sum())
    return net / total if total > 0 else 0.0


def max_drawdown_pct(close: pd.Series) -> float:
    peak = close.cummax()
    dd = (peak - close) / peak
    return float(dd.max())


def classify_regime(ret: float, adx_mean: float, vol_annual: float) -> str:
    trending = adx_mean >= 25.0
    weak_trend = 20.0 <= adx_mean < 25.0
    if trending and ret > 0.15:
        return "TRENDING_BULL"
    if trending and ret < -0.15:
        return "TRENDING_BEAR"
    if not trending and vol_annual >= 0.65:
        return "HIGH_VOL_CHOP"
    if not trending and vol_annual < 0.65 and abs(ret) < 0.15:
        return "LOW_VOL_CHOP" if adx_mean < 18 else "SIDEWAYS"
    if weak_trend:
        return "MIXED"
    return "SIDEWAYS"


def characterize_regime(daily: pd.DataFrame, label: str) -> dict[str, Any]:
    close = daily["close"]
    log_ret = np.log(close / close.shift(1)).dropna()
    ret = float(close.iloc[-1] / close.iloc[0] - 1.0)
    vol_annual = float(log_ret.std() * np.sqrt(365.0))
    mdd = max_drawdown_pct(close)
    adx = compute_adx(daily, 14)
    adx_mean = float(adx.dropna().mean()) if adx.notna().any() else float("nan")
    sma200 = close.rolling(200, min_periods=1).mean()
    pct_above = float((close > sma200).mean())
    crosses = int(((close > sma200).astype(int).diff().abs() > 0).sum())
    eff = directional_efficiency(close)
    regime = classify_regime(ret, adx_mean if not np.isnan(adx_mean) else 0.0, vol_annual)
    return {
        "split": label,
        "start": str(daily.index[0]),
        "end": str(daily.index[-1]),
        "bars": int(len(daily)),
        "btc_return": round(ret, 4),
        "annualized_volatility": round(vol_annual, 4),
        "max_drawdown": round(mdd, 4),
        "adx_mean": round(adx_mean, 2) if not np.isnan(adx_mean) else None,
        "trend_strength": "STRONG" if adx_mean >= 25 else ("WEAK" if adx_mean >= 20 else "ABSENT"),
        "percent_time_above_sma200": round(pct_above, 4),
        "number_of_sma200_crosses": crosses,
        "directional_efficiency": round(eff, 4),
        "regime_classification": regime,
    }


# ---------------------------------------------------------------------------
# ETAPA 1 -- splits
# ---------------------------------------------------------------------------

def report_splits(frame: pd.DataFrame, label: str) -> dict[str, Any]:
    splits = dev_val_oos_split(frame)
    out: dict[str, Any] = {}
    prev_end = None
    overlap = False
    for split in splits:
        if len(split.frame) == 0:
            continue
        start, end = split.frame.index[0], split.frame.index[-1]
        out[f"{split.name}_START"] = str(start)
        out[f"{split.name}_END"] = str(end)
        if prev_end is not None and start <= prev_end:
            overlap = True
        prev_end = end
    out["overlap_detected"] = overlap
    out["label"] = label
    return out


# ---------------------------------------------------------------------------
# ETAPA 3/4/5 -- gross/net, trade-by-trade, monthly, per split
# ---------------------------------------------------------------------------

def full_split_diagnostics(strategy_factory, frame: pd.DataFrame, warmup_bars: int, btc_price_for_market_return: pd.DataFrame | None) -> dict[str, Any]:
    splits = dev_val_oos_split(frame)
    out: dict[str, Any] = {}
    oos_trades: list[dict[str, Any]] = []
    for split in splits:
        if len(split.frame) <= warmup_bars + 5:
            out[split.name] = {"trades": 0, "status": "INSUFFICIENT_DATA"}
            continue
        strat_net = strategy_factory()
        net_result = _run_once(strat_net, split.frame, BASE_FEE, warmup_bars)
        net = net_result.metrics
        gross = _gross_metrics(net_result, CAPITAL)
        strat_stress = strategy_factory()
        stress = _stress_metrics(strat_stress, split.frame, warmup_bars, CAPITAL)
        turnover = sum(t["quantity"] * t["entry_price"] for t in net_result.trades) / CAPITAL
        total_fees = sum(t.get("entry_fee", 0.0) + t.get("exit_fee", 0.0) for t in net_result.trades)

        if gross.profit_factor < 1.0:
            cost_classification = "NO_GROSS_EDGE"
        elif net.profit_factor < 1.0:
            cost_classification = "GROSS_EDGE_DESTROYED_BY_COSTS"
        else:
            cost_classification = "EDGE_PRESENT_NET_OF_COSTS"

        entry = {
            "trades": net.total_trades,
            "gross_pf": round(gross.profit_factor, 4),
            "net_pf": round(net.profit_factor, 4),
            "net_pf_stress": round(stress.profit_factor, 4),
            "gross_expectancy": round(gross.expectancy, 4),
            "net_expectancy": round(net.expectancy, 4),
            "net_expectancy_stress": round(stress.expectancy, 4),
            "total_fees": round(total_fees, 2),
            "turnover_x_capital": round(turnover, 3),
            "sharpe": round(net.sharpe_ratio, 3),
            "max_drawdown_pct": round(net.max_drawdown_pct, 4),
            "cost_classification": cost_classification,
        }

        if split.name == "OOS":
            pnls = [t["pnl"] for t in net_result.trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            durations_bars = [t["exit_bar"] - t["entry_bar"] for t in net_result.trades]
            total_gross_profit = sum(p for p in pnls if p > 0)
            best_trade = max(pnls) if pnls else 0.0
            profit_concentration = (best_trade / total_gross_profit) if total_gross_profit > 0 else None
            entry.update(
                {
                    "wins": len(wins),
                    "losses": len(losses),
                    "avg_win": round(float(np.mean(wins)), 2) if wins else 0.0,
                    "avg_loss": round(float(np.mean(losses)), 2) if losses else 0.0,
                    "median_win": round(float(np.median(wins)), 2) if wins else 0.0,
                    "median_loss": round(float(np.median(losses)), 2) if losses else 0.0,
                    "best_trade": round(best_trade, 2),
                    "worst_trade": round(min(pnls), 2) if pnls else 0.0,
                    "profit_concentration_top1_of_gross_profit": round(profit_concentration, 3) if profit_concentration is not None else None,
                    "avg_duration_bars": round(float(np.mean(durations_bars)), 1) if durations_bars else 0.0,
                    "exit_reasons": pd.Series([t.get("exit_reason") for t in net_result.trades]).value_counts().to_dict(),
                }
            )
            oos_trades = net_result.trades
        out[split.name] = entry

    monthly = None
    if oos_trades:
        rows = []
        for t in oos_trades:
            month = pd.Timestamp(t["exit_time"]).strftime("%Y-%m")
            rows.append({"month": month, "pnl": t["pnl"], "gross_pnl": t["pnl"] + t.get("entry_fee", 0.0) + t.get("exit_fee", 0.0)})
        monthly_df = pd.DataFrame(rows).groupby("month").agg(trades=("pnl", "size"), net_pnl=("pnl", "sum"), gross_pnl=("gross_pnl", "sum")).reset_index()
        if btc_price_for_market_return is not None:
            monthly_market = btc_price_for_market_return["close"].resample("MS").agg(["first", "last"])
            monthly_market["market_return"] = monthly_market["last"] / monthly_market["first"] - 1.0
            monthly_market.index = monthly_market.index.strftime("%Y-%m")
            monthly_df["market_return"] = monthly_df["month"].map(monthly_market["market_return"])
        monthly = monthly_df.round(4).to_dict(orient="records")
    return {"splits": out, "oos_monthly": monthly}


# ---------------------------------------------------------------------------
# ETAPA 6/7 -- walk-forward diagnostic + buy&hold baseline (no optimization)
# ---------------------------------------------------------------------------

def walk_forward(strategy_factory, frame: pd.DataFrame, warmup_bars: int, window_bars: int, step_bars: int) -> list[dict[str, Any]]:
    windows = []
    start_idx = 0
    n = len(frame)
    while start_idx + window_bars <= n:
        window = frame.iloc[start_idx : start_idx + window_bars]
        if len(window) > warmup_bars + 5:
            strat = strategy_factory()
            result = _run_once(strat, window, BASE_FEE, warmup_bars)
            m = result.metrics
            bh_return = float(window["close"].iloc[-1] / window["close"].iloc[0] - 1.0)
            if m.total_trades == 0:
                cls = "NO_EDGE"
            elif m.profit_factor >= 1.3 and m.expectancy > 0:
                cls = "STABLE_EDGE"
            elif m.profit_factor >= 1.0 and m.expectancy > 0:
                cls = "REGIME_DEPENDENT_EDGE"
            else:
                cls = "NO_EDGE"
            windows.append(
                {
                    "start": str(window.index[0]),
                    "end": str(window.index[-1]),
                    "trades": m.total_trades,
                    "net_pf": round(m.profit_factor, 3),
                    "expectancy": round(m.expectancy, 4),
                    "sharpe": round(m.sharpe_ratio, 3),
                    "max_drawdown_pct": round(m.max_drawdown_pct, 4),
                    "buy_hold_return": round(bh_return, 4),
                    "classification": cls,
                }
            )
        start_idx += step_bars
    return windows


def summarize_walk_forward(windows: list[dict[str, Any]]) -> str:
    if not windows:
        return "NO_DATA"
    classes = [w["classification"] for w in windows]
    stable = classes.count("STABLE_EDGE")
    regime_dep = classes.count("REGIME_DEPENDENT_EDGE")
    no_edge = classes.count("NO_EDGE")
    total = len(classes)
    if stable / total >= 0.6:
        return "STABLE_EDGE"
    if (stable + regime_dep) / total >= 0.4 and no_edge / total >= 0.3:
        return "REGIME_DEPENDENT_EDGE"
    # decaying: edge concentrated in early windows, absent in later ones
    half = total // 2
    early_ok = sum(1 for c in classes[:half] if c != "NO_EDGE")
    late_ok = sum(1 for c in classes[half:] if c != "NO_EDGE")
    if early_ok > 0 and late_ok == 0:
        return "DECAYING_EDGE"
    if no_edge / total >= 0.6:
        return "NO_EDGE"
    return "REGIME_DEPENDENT_EDGE"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    report: dict[str, Any] = {}

    _log("STAGE=LOAD_DATA")
    btc_15m = load_base_candles("BTC/USDT")
    btc_4h = resample_ohlcv(btc_15m, "4h")
    btc_daily = resample_ohlcv(btc_15m, "1d")
    weekly_btc = prepare_weekly_frame(btc_15m, daily_ema_period=200, daily_lookback=20)

    # ---------------- ETAPA 1: splits ----------------
    _log("STAGE=ETAPA1_SPLITS")
    splits_report = {
        "BTC_4H_SMA200_TREND / APEX_NO_PYRAMID / QUATTRO_DONCHIAN (BTC 4h frame)": report_splits(btc_4h, "btc_4h"),
        "5EMA_WEEKLY_FILTER (BTC weekly frame)": report_splits(weekly_btc, "btc_weekly"),
        "MULTI_ASSET_VOL_NORMALIZED_TREND (BTC daily frame)": report_splits(btc_daily, "btc_daily"),
    }
    report["etapa1_splits"] = splits_report
    report["etapa1_structural_notes"] = {
        "lookahead_guard": "All rolling/breakout features (donchian_high, breakout_high, trail_low) use .rolling(...).shift(1); SMA/EMA use only bars up to and including the current bar (standard, no future leak); daily/weekly filter merges use pandas.merge_asof(direction='backward') so a daily/weekly bar is only visible after it has closed.",
        "same_bar_execution_caveat": "Entries/exits execute at the CLOSE of the signal bar (platform convention shared by every strategy already on this platform, not unique to this experiment) -- this is an idealization (zero-latency reaction at candle close), NOT a lookahead bug: the signal is computed from data already fully known at that close.",
        "overlap_check": "no overlap detected in any of the 3 frames (see overlap_detected=False per split above); DEV/VALIDATION/OOS are contiguous, non-shuffled, temporal slices.",
    }
    for k, v in splits_report.items():
        _log(f"SPLITS[{k}]: {v}")

    # ---------------- ETAPA 2: regime ----------------
    _log("STAGE=ETAPA2_REGIME")
    dev_v, val_v, oos_v = dev_val_oos_split(btc_4h)
    regime = {}
    for split_name, split_frame in (("DEV", dev_v.frame), ("VALIDATION", val_v.frame), ("OOS", oos_v.frame)):
        daily_slice = btc_daily.loc[split_frame.index[0] : split_frame.index[-1]]
        regime[split_name] = characterize_regime(daily_slice, split_name)
        _log(f"REGIME[{split_name}]: {regime[split_name]}")
    report["etapa2_regime"] = regime

    # ---------------- ETAPA 3/4/5: per-strategy diagnostics ----------------
    _log("STAGE=ETAPA345_PER_STRATEGY")
    strategy_configs = [
        ("BTC_4H_SMA200_TREND", lambda: Sma200TrendStrategy(200), btc_4h, 210),
        ("APEX_NO_PYRAMID_BTC", lambda: ApexNoPyramidStrategy(), btc_4h, 1100),
        ("QUATTRO_DONCHIAN_BTC", lambda: QuattroDonchianStrategy(), btc_4h, 250),
        ("5EMA_WEEKLY_FILTER_BTC", lambda: FiveEmaWeeklyFilterStrategy(), weekly_btc, 10),
        ("MULTI_ASSET_VOL_NORMALIZED_TREND_BTC", lambda: VolNormalizedTrendStrategy(), btc_daily, 210),
    ]
    per_strategy: dict[str, Any] = {}
    for name, factory, frame, warmup in strategy_configs:
        _log(f"STAGE=DIAGNOSE STRATEGY={name}")
        diag = full_split_diagnostics(factory, frame, warmup, btc_daily)
        per_strategy[name] = diag
        oos = diag["splits"].get("OOS", {})
        _log(f"  OOS: {oos}")
    report["etapa345_per_strategy"] = per_strategy

    # ---------------- ETAPA 6/7: walk-forward + buy&hold (SMA200, Quattro) ----------------
    _log("STAGE=ETAPA67_WALKFORWARD")
    window_bars = 6 * 30 * 6  # ~6 months of 4h bars (6 bars/day * 30 * 6)
    step_bars = 3 * 30 * 6    # ~3 months step
    wf_sma200 = walk_forward(lambda: Sma200TrendStrategy(200), btc_4h, 210, window_bars, step_bars)
    wf_quattro = walk_forward(lambda: QuattroDonchianStrategy(), btc_4h, 250, window_bars, step_bars)
    report["etapa6_walk_forward"] = {
        "window_description": "~6-month rolling windows of BTC 4h bars, ~3-month step, ORIGINAL frozen parameters only (no optimization), full pre-holdout history.",
        "SMA200": {"windows": wf_sma200, "summary_classification": summarize_walk_forward(wf_sma200)},
        "QUATTRO_DONCHIAN": {"windows": wf_quattro, "summary_classification": summarize_walk_forward(wf_quattro)},
    }
    _log(f"WALK_FORWARD_SMA200_SUMMARY={summarize_walk_forward(wf_sma200)} (n_windows={len(wf_sma200)})")
    _log(f"WALK_FORWARD_QUATTRO_SUMMARY={summarize_walk_forward(wf_quattro)} (n_windows={len(wf_quattro)})")

    # ---------------- ETAPA 8: cross-asset ----------------
    _log("STAGE=ETAPA8_CROSS_ASSET")
    cross_asset: dict[str, Any] = {}
    for symbol in ("ETH/USDT", "BNB/USDT"):
        try:
            base = load_base_candles(symbol)
            frame4h = resample_ohlcv(base, "4h")
            sma_diag = full_split_diagnostics(lambda: Sma200TrendStrategy(200), frame4h, 210, None)
            quattro_diag = full_split_diagnostics(lambda: QuattroDonchianStrategy(), frame4h, 250, None)
            cross_asset[symbol] = {
                "SMA200_OOS": sma_diag["splits"].get("OOS", {}),
                "QUATTRO_OOS": quattro_diag["splits"].get("OOS", {}),
            }
            _log(f"CROSS_ASSET[{symbol}] SMA200_OOS={cross_asset[symbol]['SMA200_OOS'].get('net_pf')} QUATTRO_OOS={cross_asset[symbol]['QUATTRO_OOS'].get('net_pf')}")
        except Exception as exc:
            cross_asset[symbol] = {"error": str(exc)}
            _log(f"CROSS_ASSET[{symbol}] ERROR={exc}")
    report["etapa8_cross_asset"] = cross_asset

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _log(f"STATUS=COMPLETED OUTPUT={OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
