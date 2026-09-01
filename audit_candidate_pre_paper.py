"""Pre-Paper audit of a FROZEN candidate strategy (no optimization, no rule
changes, FINAL_HOLDOUT never loaded, Paper Live untouched).

Default target: ExtRepl_RegimeAdaptive_SMA200_MR on BNB/USDT 4h.

Reuses, unmodified:
- RegimeAdaptiveStrategy / load_base_candles / resample_ohlcv / dev_val_oos_split
  / BASE_FEE / CAPITAL (research/external_strategy_replication_strategies.py)
- _run_once (run_external_strategy_replication.py)
- walk_forward (diagnose_oos_failure.py)
- _profit_factor / BOOTSTRAP_ITERATIONS (strategy_discovery_cycle1.py)

Read-only: writes no registry, no report file.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from diagnose_oos_failure import walk_forward
from research.external_strategy_replication_strategies import (
    BASE_FEE,
    CAPITAL,
    RegimeAdaptiveStrategy,
    Sma200RegimeGatedStrategy,
    dev_val_oos_split,
    load_base_candles,
    resample_ohlcv,
)
from run_external_strategy_replication import _run_once
from strategy_discovery_cycle1 import BOOTSTRAP_ITERATIONS, _profit_factor

SYMBOL = "BNB/USDT"
TIMEFRAME = "4h"
WARMUP = 210
WINDOW_BARS = 6 * 30 * 6
STEP_BARS = 3 * 30 * 6
PARAMS = {"sma_period": 200, "adx_period": 14, "adx_threshold": 20.0, "slope_lookback": 20}
OUTPUT: list[str] = []


def _log(msg: str) -> None:
    OUTPUT.append(msg)


def _pnls(trades: list[dict[str, Any]]) -> np.ndarray:
    return np.asarray([t["pnl"] for t in trades], dtype=float)


def _stats(pnl: np.ndarray) -> dict[str, float]:
    return {
        "trades": int(len(pnl)),
        "net_pf": round(_profit_factor(pnl), 3),
        "expectancy": round(float(pnl.mean()), 4) if len(pnl) else 0.0,
        "net_pnl": round(float(pnl.sum()), 2),
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    frame = resample_ohlcv(load_base_candles(SYMBOL), TIMEFRAME)
    _log(f"FRAME bars={len(frame)} start={frame.index[0]} end={frame.index[-1]}")

    # --- base run over the full pre-holdout history (DEV+VAL+OOS contiguous) ---
    base = _run_once(RegimeAdaptiveStrategy(**PARAMS), frame, BASE_FEE, WARMUP)
    trades = base.trades
    pnl = _pnls(trades)
    _log(f"ETAPA0_FULL_HISTORY={_stats(pnl)} sharpe={round(base.metrics.sharpe_ratio,3)} "
         f"maxdd={round(base.metrics.max_drawdown_pct,4)}")

    # --- ETAPA 2: walk-forward, frozen params, same windows as before ---
    _log("STAGE=ETAPA2_WALK_FORWARD")
    windows = walk_forward(lambda: RegimeAdaptiveStrategy(**PARAMS), frame, WARMUP, WINDOW_BARS, STEP_BARS)
    for w in windows:
        _log(f"  WF {w['start'][:10]}->{w['end'][:10]} trades={w['trades']} pf={w['net_pf']} "
             f"exp={w['expectancy']} sharpe={w['sharpe']} dd={w['max_drawdown_pct']} "
             f"net_pnl={round(w['expectancy'] * w['trades'], 2)} cls={w['classification']}")
    pfs = [w["net_pf"] for w in windows]
    exps = [w["expectancy"] for w in windows]
    pos = sum(1 for w in windows if w["net_pf"] > 1.0 and w["expectancy"] > 0)
    _log(f"WF_SUMMARY total={len(windows)} positive={pos} negative={len(windows)-pos} "
         f"median_pf={round(float(np.median(pfs)),3)} avg_pf={round(float(np.mean(pfs)),3)} "
         f"median_exp={round(float(np.median(exps)),4)} worst_pf={round(min(pfs),3)}")

    # --- ETAPA 3: profit concentration ---
    _log("STAGE=ETAPA3_PROFIT_CONCENTRATION")
    order = np.argsort(pnl)[::-1]
    gross_profit = float(pnl[pnl > 0].sum())
    top_n_share = lambda n: round(float(pnl[order[:n]].sum()) / gross_profit, 4) if gross_profit > 0 else 0.0
    n10 = max(1, int(round(0.10 * len(pnl))))
    _log(f"TOTAL_TRADES={len(pnl)} TOP1={top_n_share(1)} TOP3={top_n_share(3)} "
         f"TOP5={top_n_share(5)} TOP10PCT({n10})={top_n_share(n10)}")
    _log(f"WITHOUT_BEST={_stats(np.delete(pnl, order[:1]))}")
    _log(f"WITHOUT_TOP3={_stats(np.delete(pnl, order[:3]))}")

    # --- ETAPA 4: temporal stability ---
    _log("STAGE=ETAPA4_TEMPORAL_STABILITY")
    tdf = pd.DataFrame({"pnl": pnl, "exit": pd.to_datetime([t["exit_time"] for t in trades], utc=True)})
    for gran, freq in (("MONTHLY", "M"), ("QUARTERLY", "Q")):
        g = tdf.groupby(tdf["exit"].dt.to_period(freq))["pnl"]
        agg = g.agg(["count", "sum"])
        med_trades = float(agg["count"].median())
        pos_p = int((agg["sum"] > 0).sum())
        neg_p = int((agg["sum"] <= 0).sum())
        _log(f"{gran} periods={len(agg)} median_trades={med_trades} positive={pos_p} negative={neg_p} "
             f"pct_positive={round(100.0*pos_p/max(len(agg),1),1)} "
             f"worst={agg['sum'].idxmin()}:{round(float(agg['sum'].min()),2)} "
             f"best={agg['sum'].idxmax()}:{round(float(agg['sum'].max()),2)}")

    # --- ETAPA 5 / 6: components + regimes ---
    _log("STAGE=ETAPA5_6_COMPONENTS_REGIMES")
    strat = RegimeAdaptiveStrategy(**PARAMS)
    strat.initialize()
    feats = strat.calculate(frame)
    bull = feats["regime_bull"].to_numpy(dtype=bool)
    side = feats["regime_sideways"].to_numpy(dtype=bool)
    live = slice(WARMUP, len(frame))
    n_live = len(frame) - WARMUP
    _log(f"TIME_IN_TREND={round(float(bull[live].sum())/n_live,4)} "
         f"TIME_IN_SIDEWAYS={round(float(side[live].sum())/n_live,4)} "
         f"TIME_IN_CASH={round(1.0 - float((bull[live] | side[live]).sum())/n_live,4)}")
    ent = np.asarray([t["entry_bar"] for t in trades], dtype=int)
    is_trend = bull[ent]
    is_mr = side[ent] & ~is_trend
    other = ~(is_trend | is_mr)
    _log(f"TREND_FOLLOWING={_stats(pnl[is_trend])}")
    _log(f"MEAN_REVERSION={_stats(pnl[is_mr])}")
    _log(f"ENTRIES_IN_NEITHER_REGIME={int(other.sum())} (must be 0 -> TRENDING_BEAR must be CASH)")
    _log(f"COMBINED={_stats(pnl)}")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    _log(f"EXIT_REASONS={reasons}")

    # --- ETAPA 7: local parameter perturbation (NOT optimization) ---
    _log("STAGE=ETAPA7_PARAMETER_ROBUSTNESS")
    variants = [("ORIGINAL", PARAMS)]
    for key, lo, hi in (("sma_period", 180, 220), ("adx_threshold", 18.0, 22.0), ("slope_lookback", 18, 22)):
        for label, val in ((f"{key}-10%", lo), (f"{key}+10%", hi)):
            variants.append((label, {**PARAMS, key: val}))
    robustness = {}
    for label, params in variants:
        r = _run_once(RegimeAdaptiveStrategy(**params), frame, BASE_FEE, WARMUP)
        s = _stats(_pnls(r.trades))
        robustness[label] = s
        _log(f"  {label}: {s}")
    pf_variants = [v["net_pf"] for k, v in robustness.items() if k != "ORIGINAL"]
    n_above1 = sum(1 for p in pf_variants if p > 1.0)
    orig_pf = robustness["ORIGINAL"]["net_pf"]
    rel_drop = max((orig_pf - p) / orig_pf for p in pf_variants) if orig_pf > 0 else 1.0
    if n_above1 == len(pf_variants) and rel_drop <= 0.30:
        rob_cls = "ROBUST_PLATEAU"
    elif n_above1 >= len(pf_variants) - 1 and rel_drop <= 0.60:
        rob_cls = "MODERATELY_SENSITIVE"
    else:
        rob_cls = "FRAGILE"
    _log(f"PARAM_ROBUSTNESS variants_pf>1={n_above1}/{len(pf_variants)} max_rel_pf_drop={round(rel_drop,3)} -> {rob_cls}")

    # --- ETAPA 8: cost stress (diagnostic only, official model unchanged) ---
    _log("STAGE=ETAPA8_COST_STRESS")
    for label, mult in (("BASE", 1.0), ("1.25x", 1.25), ("1.50x", 1.50)):
        r = base if mult == 1.0 else _run_once(RegimeAdaptiveStrategy(**PARAMS), frame, BASE_FEE * mult, WARMUP)
        _log(f"  COST_{label} fee={round(BASE_FEE*mult,5)}/side {_stats(_pnls(r.trades))}")

    # --- ETAPA 9: bootstrap (reuses project convention: 10k resamples, seeded) ---
    _log("STAGE=ETAPA9_BOOTSTRAP")
    rng = np.random.default_rng(20260818)
    samples = pnl[rng.integers(0, len(pnl), size=(BOOTSTRAP_ITERATIONS, len(pnl)))]
    pf_b = np.asarray([_profit_factor(s) for s in samples])
    exp_b = samples.mean(axis=1)
    _log(f"PF_MEDIAN={round(float(np.median(pf_b)),3)} "
         f"PF_CI95=[{round(float(np.percentile(pf_b,2.5)),3)}, {round(float(np.percentile(pf_b,97.5)),3)}] "
         f"EXP_MEDIAN={round(float(np.median(exp_b)),4)} "
         f"EXP_CI95=[{round(float(np.percentile(exp_b,2.5)),4)}, {round(float(np.percentile(exp_b,97.5)),4)}] "
         f"P(PF>1)={round(float((pf_b > 1.0).mean()),4)}")

    # --- OOS-only reference (already known, recomputed for consistency) ---
    for split in dev_val_oos_split(frame):
        r = _run_once(RegimeAdaptiveStrategy(**PARAMS), split.frame, BASE_FEE, WARMUP)
        _log(f"SPLIT_{split.name}={_stats(_pnls(r.trades))} sharpe={round(r.metrics.sharpe_ratio,3)} "
             f"maxdd={round(r.metrics.max_drawdown_pct,4)}")

    _log("DONE FINAL_HOLDOUT_USED=NO PAPER_LIVE_TOUCHED=NO")
    print("\n".join(OUTPUT), flush=True)
    return 0


def _comparison_metrics(result) -> dict[str, float]:
    metrics = _stats(_pnls(result.trades))
    return {
        **metrics,
        "sharpe": round(result.metrics.sharpe_ratio, 3),
        "max_drawdown": round(result.metrics.max_drawdown_pct, 4),
    }


def _wf_metrics(factory, frame: pd.DataFrame) -> dict[str, float]:
    windows = walk_forward(factory, frame, WARMUP, WINDOW_BARS, STEP_BARS)
    positive = sum(1 for window in windows if window["net_pf"] > 1.0 and window["expectancy"] > 0)
    return {
        "positive": positive,
        "negative": len(windows) - positive,
        "median_pf": round(float(np.median([window["net_pf"] for window in windows])), 3),
    }


def final_b_audit() -> int:
    logging.disable(logging.CRITICAL)
    frame = resample_ohlcv(load_base_candles(SYMBOL), TIMEFRAME)
    factory = lambda params=PARAMS: Sma200RegimeGatedStrategy(**params)
    result = _run_once(factory(), frame, BASE_FEE, WARMUP)
    pnl = _pnls(result.trades)
    order = np.argsort(pnl)[::-1]
    gross_profit = float(pnl[pnl > 0].sum())
    share = lambda count: round(float(pnl[order[:count]].sum()) / gross_profit, 4) if gross_profit else 0.0
    print(f"TOTAL_TRADES={len(pnl)} TOP1_PROFIT_SHARE={share(1)} TOP3_PROFIT_SHARE={share(3)}")
    print(f"PF_WITHOUT_BEST={_stats(np.delete(pnl, order[:1]))['net_pf']} "
          f"PF_WITHOUT_TOP3={_stats(np.delete(pnl, order[:3]))['net_pf']}")

    temporal = pd.DataFrame({"pnl": pnl, "exit": pd.to_datetime([trade["exit_time"] for trade in result.trades], utc=True)})
    periods = temporal.groupby(temporal["exit"].dt.to_period("Q"))["pnl"].sum()
    positive = int((periods > 0).sum())
    print(f"TEMPORAL_STABILITY=QUARTERLY positive={positive} negative={len(periods)-positive} "
          f"pct_positive={round(100 * positive / len(periods), 1)} "
          f"worst={periods.idxmin()}:{round(float(periods.min()), 2)} "
          f"best={periods.idxmax()}:{round(float(periods.max()), 2)}")

    for label, multiplier in (("BASE", 1.0), ("1_25X", 1.25), ("1_50X", 1.50)):
        stressed = result if multiplier == 1.0 else _run_once(factory(), frame, BASE_FEE * multiplier, WARMUP)
        print(f"COST_{label}_PF={_stats(_pnls(stressed.trades))['net_pf']}")

    variants = [("ORIGINAL", PARAMS)]
    for key, low, high in (("sma_period", 180, 220), ("adx_threshold", 18.0, 22.0), ("slope_lookback", 18, 22)):
        variants.extend(((f"{key}-10%", {**PARAMS, key: low}), (f"{key}+10%", {**PARAMS, key: high})))
    variant_pf = {}
    for label, params in variants:
        variant_pf[label] = _stats(_pnls(_run_once(factory(params), frame, BASE_FEE, WARMUP).trades))["net_pf"]
    altered = [pf for label, pf in variant_pf.items() if label != "ORIGINAL"]
    maximum_drop = max((variant_pf["ORIGINAL"] - pf) / variant_pf["ORIGINAL"] for pf in altered)
    classification = "ROBUST_PLATEAU" if all(pf > 1.0 for pf in altered) and maximum_drop <= 0.30 else "MODERATELY_SENSITIVE" if sum(pf > 1.0 for pf in altered) >= 5 else "FRAGILE"
    print(f"PARAMETER_ROBUSTNESS={classification} pf_variants={variant_pf} max_rel_pf_drop={round(maximum_drop, 3)}")

    rng = np.random.default_rng(20260818)
    samples = pnl[rng.integers(0, len(pnl), size=(BOOTSTRAP_ITERATIONS, len(pnl)))]
    pf_samples = np.asarray([_profit_factor(sample) for sample in samples])
    exp_samples = samples.mean(axis=1)
    print(f"BOOTSTRAP_PF_CI95=[{round(float(np.percentile(pf_samples, 2.5)), 3)}, {round(float(np.percentile(pf_samples, 97.5)), 3)}] "
          f"BOOTSTRAP_EXPECTANCY_CI95=[{round(float(np.percentile(exp_samples, 2.5)), 4)}, {round(float(np.percentile(exp_samples, 97.5)), 4)}] "
          f"PROBABILITY_PF_GT_1={round(float((pf_samples > 1.0).mean()), 4)}")
    print("FINAL_HOLDOUT_USED=NO PAPER_LIVE_TOUCHED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(final_b_audit())
