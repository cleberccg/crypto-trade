"""Busca de candidatos em 1h: no máximo 3 estratégias públicas de regras congeladas, BTC/ETH/BNB 1h,
critério de frequência -> DEV -> VALIDATION -> OOS e, em seguida, robustez para UM candidato.
Reutiliza as partições/carregadores/engine existentes; FINAL_HOLDOUT nunca é carregado."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from audit_candidate_pre_paper import _pnls, _stats
from diagnose_oos_failure import walk_forward
from research.external_strategy_replication_strategies import (
    BASE_FEE,
    ConnorsRsi2Strategy,
    DonchianTurtleS1Strategy,
    IbsMeanReversionStrategy,
    dev_val_oos_split,
    load_base_candles,
    resample_ohlcv,
)
from run_external_strategy_replication import _run_once, evaluate_split
from run_mean_reversion_candidate_search import CandidateSpec, candidate_ok, positive
from strategy_discovery_cycle1 import BOOTSTRAP_ITERATIONS, _profit_factor

SYMBOLS = ("BTC/USDT", "ETH/USDT", "BNB/USDT")
TIMEFRAME = "1h"
WARMUP = 220
MIN_TRADES_PER_MONTH = 7.0
WINDOW_BARS = 6 * 30 * 24
STEP_BARS = 3 * 30 * 24

SPECS = (
    CandidateSpec(
        name="CONNORS_RSI2",
        source="Connors & Alvarez, 'Short Term Trading Strategies That Work' (2008): close>SMA200, RSI(2)<10 buy, exit close>SMA5",
        factory=lambda: ConnorsRsi2Strategy(),
        params={"trend_period": 200, "rsi_period": 2, "rsi_entry": 10.0, "exit_period": 5},
    ),
    CandidateSpec(
        name="DONCHIAN_TURTLE_S1",
        source="Turtle Trading System 1 (Faith, 'Way of the Turtle'): close>20-bar high buy, close<10-bar low exit",
        factory=lambda: DonchianTurtleS1Strategy(),
        params={"entry_period": 20, "exit_period": 10},
    ),
    CandidateSpec(
        name="IBS_MEAN_REVERSION",
        source="Internal Bar Strength reversal (Pagonidis 2014; Quantpedia): IBS<0.2 buy, IBS>0.8 exit",
        factory=lambda: IbsMeanReversionStrategy(),
        params={"entry_ibs": 0.2, "exit_ibs": 0.8},
    ),
)

SENSITIVITY: dict[str, list[tuple[str, Callable[[], Any]]]] = {
    "CONNORS_RSI2": [
        ("trend_period-10%", lambda: ConnorsRsi2Strategy(trend_period=180)),
        ("trend_period+10%", lambda: ConnorsRsi2Strategy(trend_period=220)),
        ("rsi_entry-10%", lambda: ConnorsRsi2Strategy(rsi_entry=9.0)),
        ("rsi_entry+10%", lambda: ConnorsRsi2Strategy(rsi_entry=11.0)),
    ],
    "DONCHIAN_TURTLE_S1": [
        ("entry_period-10%", lambda: DonchianTurtleS1Strategy(entry_period=18)),
        ("entry_period+10%", lambda: DonchianTurtleS1Strategy(entry_period=22)),
        ("exit_period-10%", lambda: DonchianTurtleS1Strategy(exit_period=9)),
        ("exit_period+10%", lambda: DonchianTurtleS1Strategy(exit_period=11)),
    ],
    "IBS_MEAN_REVERSION": [
        ("entry_ibs-10%", lambda: IbsMeanReversionStrategy(entry_ibs=0.18)),
        ("entry_ibs+10%", lambda: IbsMeanReversionStrategy(entry_ibs=0.22)),
        ("exit_ibs-10%", lambda: IbsMeanReversionStrategy(exit_ibs=0.72)),
        ("exit_ibs+10%", lambda: IbsMeanReversionStrategy(exit_ibs=0.88)),
    ],
}

_FRAMES: dict[str, pd.DataFrame] = {}


def frame_for(symbol: str) -> pd.DataFrame:
    if symbol not in _FRAMES:
        _FRAMES[symbol] = resample_ohlcv(load_base_candles(symbol), TIMEFRAME)
    return _FRAMES[symbol]


def evaluate_symbol(spec: CandidateSpec, symbol: str) -> dict[str, Any]:
    frame = frame_for(symbol)
    full = _run_once(spec.factory(), frame, BASE_FEE, WARMUP)
    months = (frame.index[-1] - frame.index[WARMUP]).days / 30.44
    tpm = len(full.trades) / months if months > 0 else 0.0
    out: dict[str, Any] = {"strategy": spec.name, "symbol": symbol, "historical_trades": len(full.trades), "trades_per_month": round(tpm, 2), "splits": {}, "candidate": False}
    if tpm < MIN_TRADES_PER_MONTH:
        out["status"] = "REJECTED_LOW_SIGNAL_FREQUENCY"
        return out
    for split in dev_val_oos_split(frame):
        metrics = evaluate_split(spec.factory, split.frame, WARMUP)
        metrics["status"] = "POSITIVE" if positive(metrics) else "NOT_POSITIVE"
        out["splits"][split.name] = metrics
        if metrics["status"] != "POSITIVE":
            out["status"] = f"REJECTED_{split.name}_NOT_POSITIVE"
            return out
    out["candidate"] = candidate_ok(out["splits"])
    out["status"] = "CANDIDATE" if out["candidate"] else "REJECTED_CANDIDATE_GATE"
    return out


def robustness(spec: CandidateSpec, symbol: str) -> dict[str, Any]:
    frame = frame_for(symbol)
    base = _run_once(spec.factory(), frame, BASE_FEE, WARMUP)
    pnl = _pnls(base.trades)
    order = np.argsort(pnl)[::-1]
    gross_profit = float(pnl[pnl > 0].sum())
    top1_share = float(pnl[order[0]] / gross_profit) if gross_profit > 0 else 1.0
    top3_share = float(pnl[order[:3]].sum() / gross_profit) if gross_profit > 0 else 1.0
    without_top1 = _stats(np.delete(pnl, order[:1]))
    without_top3 = _stats(np.delete(pnl, order[:3]))

    windows = walk_forward(spec.factory, frame, WARMUP, WINDOW_BARS, STEP_BARS)
    wf_pos = sum(1 for w in windows if w["net_pf"] > 1.0 and w["expectancy"] > 0)
    wf_pfs = [float(w["net_pf"]) for w in windows]
    median_wf = float(np.median(wf_pfs)) if wf_pfs else 0.0

    cost_125 = _stats(_pnls(_run_once(spec.factory(), frame, BASE_FEE * 1.25, WARMUP).trades))
    cost_150 = _stats(_pnls(_run_once(spec.factory(), frame, BASE_FEE * 1.50, WARMUP).trades))

    rng = np.random.default_rng(20260928)
    samples = pnl[rng.integers(0, len(pnl), size=(BOOTSTRAP_ITERATIONS, len(pnl)))]
    pf_samples = np.asarray([_profit_factor(s) for s in samples])
    ci = [round(float(np.percentile(pf_samples, 2.5)), 3), round(float(np.percentile(pf_samples, 97.5)), 3)]

    sens: dict[str, dict[str, Any]] = {}
    for label, factory in [("ORIGINAL", spec.factory), *SENSITIVITY[spec.name]]:
        st = _stats(_pnls(_run_once(factory(), frame, BASE_FEE, WARMUP).trades))
        sens[label] = {"pf": st["net_pf"], "expectancy": st["expectancy"], "trades": st["trades"]}
    alt = [v["pf"] for k, v in sens.items() if k != "ORIGINAL"]
    param_robust = "ROBUST" if alt and all(pf > 1.0 for pf in alt) and min(alt) >= 0.70 * sens["ORIGINAL"]["pf"] else "FRAGILE"

    common = without_top3["net_pf"] > 1.0 and cost_150["net_pf"] > 1.0 and median_wf > 1.0 and param_robust == "ROBUST" and top1_share < 0.5
    if common and wf_pos > len(windows) / 2 and ci[0] > 1.0:
        classification = "ROBUST"
    elif common and wf_pos >= len(windows) / 2 and ci[0] > 0.9:
        classification = "PROMISING"
    elif _stats(pnl)["net_pf"] > 1.0:
        classification = "FRAGILE"
    else:
        classification = "REJECTED"

    return {
        "full": _stats(pnl) | {"sharpe": round(base.metrics.sharpe_ratio, 3), "max_drawdown": round(base.metrics.max_drawdown_pct, 4)},
        "top1_share_gross_profit": round(top1_share, 4),
        "top3_share_gross_profit": round(top3_share, 4),
        "pf_without_top1": without_top1["net_pf"],
        "pf_without_top3": without_top3["net_pf"],
        "walk_forward": {"windows": len(windows), "positive": wf_pos, "negative": len(windows) - wf_pos, "median_pf": round(median_wf, 3)},
        "cost_1_25x_pf": cost_125["net_pf"],
        "cost_1_50x_pf": cost_150["net_pf"],
        "bootstrap_pf_ci95": ci,
        "parameter_sensitivity": sens,
        "parameter_robustness": param_robust,
        "final_classification": classification,
        "ready_for_paper": classification in {"ROBUST", "PROMISING"},
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-json", default="")
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    for spec in SPECS:
        per_symbol = []
        for symbol in SYMBOLS:
            res = evaluate_symbol(spec, symbol)
            per_symbol.append(res)
            splits = "; ".join(f"{k}=n:{v['trades']} pf:{v['net_pf']:.3f} exp:{v['net_expectancy']:.3f} sh:{v['sharpe']:.2f} dd:{v['max_drawdown_pct']:.4f}" for k, v in res["splits"].items())
            print(f"{spec.name} {symbol} trades={res['historical_trades']} tpm={res['trades_per_month']} {res['status']} {splits}", flush=True)
        results.extend(per_symbol)
        candidates = [r for r in per_symbol if r["candidate"]]
        if not candidates:
            continue
        chosen = max(candidates, key=lambda r: r["splits"]["VALIDATION"]["net_pf"])
        robust = robustness(spec, chosen["symbol"])
        print(f"ROBUSTNESS {spec.name} {chosen['symbol']} {json.dumps(robust, default=str)}", flush=True)
        if args.candidate_json:
            Path(args.candidate_json).write_text(json.dumps({"selected": chosen, "spec_params": spec.params, "source": spec.source, "robust": robust, "all": results, "final_holdout_used": False}, indent=2, default=str), encoding="utf-8")
        if robust["ready_for_paper"]:
            print(f"SELECTED={spec.name} SYMBOL={chosen['symbol']} READY_FOR_PAPER=YES", flush=True)
            return 0
        print(f"{spec.name} {chosen['symbol']} classification={robust['final_classification']} -> not ready, next reference", flush=True)
    print("STOP: NO_1H_CANDIDATE_FOUND", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
