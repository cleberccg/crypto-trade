from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from audit_candidate_pre_paper import WINDOW_BARS, STEP_BARS, _pnls, _stats
from diagnose_oos_failure import walk_forward
from research.external_strategy_replication_strategies import (
    BASE_FEE,
    CAPITAL,
    CANDIDATE_MIN_NET_PF,
    CANDIDATE_MIN_TRADES_PER_SPLIT,
    MovingAverageDeviationReversalStrategy,
    SimpleBollingerRsiMeanReversionStrategy,
    ZScoreMeanReversionStrategy,
    dev_val_oos_split,
    load_base_candles,
    resample_ohlcv,
)
from run_external_strategy_replication import _run_once, evaluate_split
from strategy_discovery_cycle1 import BOOTSTRAP_ITERATIONS, _profit_factor

SYMBOLS = ("BTC/USDT", "ETH/USDT", "BNB/USDT")
TIMEFRAME = "4h"
WARMUP = 60


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    source: str
    factory: Callable[[], Any]
    params: dict[str, Any]


SPECS = (
    CandidateSpec(
        name="BOLLINGER_RSI_MEAN_REVERSION",
        source="Public Bollinger Bands mean-reversion template + Wilder RSI oversold/overbought confirmation",
        factory=lambda: SimpleBollingerRsiMeanReversionStrategy(),
        params={"bb_period": 20, "bb_std_dev": 2.0, "rsi_period": 14, "rsi_entry": 30.0, "rsi_exit": 70.0},
    ),
    CandidateSpec(
        name="ZSCORE_MEAN_REVERSION",
        source="Public rolling z-score mean-reversion template: enter below -2 sigma, exit at mean",
        factory=lambda: ZScoreMeanReversionStrategy(),
        params={"lookback": 20, "entry_z": -2.0, "exit_z": 0.0},
    ),
    CandidateSpec(
        name="MA_DEVIATION_REVERSAL",
        source="Public deviation-from-moving-average reversal template: enter 5% below SMA50, exit at SMA50",
        factory=lambda: MovingAverageDeviationReversalStrategy(),
        params={"ma_period": 50, "entry_deviation": -0.05, "exit_deviation": 0.0},
    ),
)


def positive(metrics: dict[str, Any]) -> bool:
    return bool(metrics.get("trades", 0) > 0 and metrics.get("net_pf", 0.0) > 1.0 and metrics.get("net_expectancy", 0.0) > 0.0)


def candidate_ok(splits: dict[str, dict[str, Any]]) -> bool:
    return (
        positive(splits["DEV"])
        and positive(splits["VALIDATION"])
        and positive(splits["OOS"])
        and splits["OOS"]["net_pf"] >= CANDIDATE_MIN_NET_PF
        and all(splits[name]["trades"] >= CANDIDATE_MIN_TRADES_PER_SPLIT for name in ("DEV", "VALIDATION", "OOS"))
    )


def evaluate_symbol(spec: CandidateSpec, symbol: str) -> dict[str, Any]:
    frame = resample_ohlcv(load_base_candles(symbol), TIMEFRAME)
    splits: dict[str, dict[str, Any]] = {}
    for split in dev_val_oos_split(frame):
        metrics = evaluate_split(spec.factory, split.frame, WARMUP)
        metrics["status"] = "POSITIVE" if positive(metrics) else "NOT_POSITIVE"
        splits[split.name] = metrics
        if split.name in {"DEV", "VALIDATION"} and metrics["status"] != "POSITIVE":
            break
    return {
        "strategy": spec.name,
        "source": spec.source,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "parameters": spec.params,
        "bars": len(frame),
        "start": frame.index[0].isoformat(),
        "end": frame.index[-1].isoformat(),
        "splits": splits,
        "candidate": set(splits) == {"DEV", "VALIDATION", "OOS"} and candidate_ok(splits),
    }


def robustness(spec: CandidateSpec, symbol: str) -> dict[str, Any]:
    frame = resample_ohlcv(load_base_candles(symbol), TIMEFRAME)
    base = _run_once(spec.factory(), frame, BASE_FEE, WARMUP)
    pnl = _pnls(base.trades)
    order = np.argsort(pnl)[::-1]
    without_top3 = _stats(np.delete(pnl, order[:3])) if len(pnl) >= 3 else {"net_pf": 0.0}

    windows = walk_forward(spec.factory, frame, WARMUP, WINDOW_BARS, STEP_BARS)
    positive_windows = sum(1 for window in windows if window["net_pf"] > 1.0 and window["expectancy"] > 0)
    pfs = [float(window["net_pf"]) for window in windows]

    cost_125 = _stats(_pnls(_run_once(spec.factory(), frame, BASE_FEE * 1.25, WARMUP).trades))
    cost_150 = _stats(_pnls(_run_once(spec.factory(), frame, BASE_FEE * 1.50, WARMUP).trades))

    rng = np.random.default_rng(20260902)
    samples = pnl[rng.integers(0, len(pnl), size=(BOOTSTRAP_ITERATIONS, len(pnl)))] if len(pnl) else np.asarray([])
    pf_samples = np.asarray([_profit_factor(sample) for sample in samples]) if len(pnl) else np.asarray([0.0])

    sensitivity = parameter_sensitivity(spec, frame)
    altered_pf = [row["pf"] for row in sensitivity.values() if row["label"] != "ORIGINAL"]
    parameter_robustness = "ROBUST" if altered_pf and all(pf > 1.0 for pf in altered_pf) and min(altered_pf) >= 0.70 * sensitivity["ORIGINAL"]["pf"] else "FRAGILE"
    final_classification = "READY_FOR_PAPER" if positive_windows > len(windows) / 2 and without_top3["net_pf"] > 1.0 and cost_150["net_pf"] > 1.0 and float(np.percentile(pf_samples, 2.5)) > 1.0 and parameter_robustness == "ROBUST" else "REJECTED"

    return {
        "full": _stats(pnl) | {"sharpe": round(base.metrics.sharpe_ratio, 3), "max_drawdown": round(base.metrics.max_drawdown_pct, 4)},
        "walk_forward": {"positive": positive_windows, "negative": len(windows) - positive_windows, "median_pf": round(float(np.median(pfs)), 3) if pfs else 0.0},
        "pf_without_top3": without_top3["net_pf"],
        "cost_1_25x_pf": cost_125["net_pf"],
        "cost_1_50x_pf": cost_150["net_pf"],
        "bootstrap_pf_ci95": [round(float(np.percentile(pf_samples, 2.5)), 3), round(float(np.percentile(pf_samples, 97.5)), 3)],
        "parameter_sensitivity": sensitivity,
        "parameter_robustness": parameter_robustness,
        "final_classification": final_classification,
    }


def parameter_sensitivity(spec: CandidateSpec, frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    variants: list[tuple[str, Callable[[], Any]]] = [("ORIGINAL", spec.factory)]
    if spec.name == "BOLLINGER_RSI_MEAN_REVERSION":
        variants.extend([
            ("bb_period-10%", lambda: SimpleBollingerRsiMeanReversionStrategy(bb_period=18)),
            ("bb_period+10%", lambda: SimpleBollingerRsiMeanReversionStrategy(bb_period=22)),
            ("rsi_entry-10%", lambda: SimpleBollingerRsiMeanReversionStrategy(rsi_entry=27.0)),
            ("rsi_entry+10%", lambda: SimpleBollingerRsiMeanReversionStrategy(rsi_entry=33.0)),
        ])
    elif spec.name == "ZSCORE_MEAN_REVERSION":
        variants.extend([
            ("lookback-10%", lambda: ZScoreMeanReversionStrategy(lookback=18)),
            ("lookback+10%", lambda: ZScoreMeanReversionStrategy(lookback=22)),
            ("entry_z-10%", lambda: ZScoreMeanReversionStrategy(entry_z=-1.8)),
            ("entry_z+10%", lambda: ZScoreMeanReversionStrategy(entry_z=-2.2)),
        ])
    else:
        variants.extend([
            ("ma_period-10%", lambda: MovingAverageDeviationReversalStrategy(ma_period=45)),
            ("ma_period+10%", lambda: MovingAverageDeviationReversalStrategy(ma_period=55)),
            ("entry_deviation-10%", lambda: MovingAverageDeviationReversalStrategy(entry_deviation=-0.045)),
            ("entry_deviation+10%", lambda: MovingAverageDeviationReversalStrategy(entry_deviation=-0.055)),
        ])
    out: dict[str, dict[str, Any]] = {}
    for label, factory in variants:
        result = _run_once(factory(), frame, BASE_FEE, WARMUP)
        stats = _stats(_pnls(result.trades))
        out[label] = {"label": label, "pf": stats["net_pf"], "expectancy": stats["expectancy"], "trades": stats["trades"]}
    return out


def main() -> int:
    logging.disable(logging.CRITICAL)
    for logger_name in ("backtesting", "risk", "strategies", "database"):
        logging.getLogger(logger_name).disabled = True
    parser = argparse.ArgumentParser(description="Mean-reversion candidate search with frozen public rules.")
    parser.add_argument("--candidate-json", default="", help="Optional path for structured selected-candidate audit output.")
    args = parser.parse_args()
    print("EXTERNAL_STRATEGIES_CHECKED:")
    selected: dict[str, Any] | None = None
    selected_spec: CandidateSpec | None = None
    for spec in SPECS:
        print(f"- {spec.name}: {spec.source}")
        for symbol in SYMBOLS:
            result = evaluate_symbol(spec, symbol)
            split_text = "; ".join(
                f"{name}=trades:{metrics['trades']} pf:{metrics['net_pf']:.3f} exp:{metrics['net_expectancy']:.4f} {metrics['status']}"
                for name, metrics in result["splits"].items()
            )
            print(f"  {symbol} {split_text} CANDIDATE={'YES' if result['candidate'] else 'NO'}")
            if result["candidate"]:
                selected = result
                selected_spec = spec
                break
        if selected is not None:
            break

    if selected is None or selected_spec is None:
        print("FINAL_STATUS=NO_CANDIDATE_FOUND")
        print("FINAL_HOLDOUT_USED=NO")
        print("READY_FOR_PAPER=NO")
        return 0

    robust = robustness(selected_spec, selected["symbol"])
    structured = {"selected": selected, "robust": robust, "final_holdout_used": False}
    if args.candidate_json:
        Path(args.candidate_json).write_text(json.dumps(structured, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("SELECTED_STRATEGY=%s" % selected["strategy"])
    print("SOURCE_REFERENCE=%s" % selected["source"])
    print("SYMBOL=%s" % selected["symbol"])
    print("TIMEFRAME=%s" % selected["timeframe"])
    for split_name in ("DEV", "VALIDATION", "OOS"):
        metrics = selected["splits"][split_name]
        print("%s_NET_PF=%.3f EXPECTANCY=%.4f SHARPE=%.3f MAX_DD=%.4f TRADES=%d" % (split_name, metrics["net_pf"], metrics["net_expectancy"], metrics["sharpe"], metrics["max_drawdown_pct"], metrics["trades"]))
    print("WALK_FORWARD=%s" % robust["walk_forward"])
    print("PF_WITHOUT_TOP3=%s" % robust["pf_without_top3"])
    print("COST_1_50X_PF=%s" % robust["cost_1_50x_pf"])
    print("BOOTSTRAP_PF_CI95=%s" % robust["bootstrap_pf_ci95"])
    print("PARAMETER_ROBUSTNESS=%s %s" % (robust["parameter_robustness"], robust["parameter_sensitivity"]))
    print("FINAL_CLASSIFICATION=%s" % robust["final_classification"])
    print("READY_FOR_PAPER=%s" % ("YES" if robust["final_classification"] == "READY_FOR_PAPER" else "NO"))
    print("FINAL_HOLDOUT_USED=NO")
    print("GENERATED_AT=%s" % datetime.now(tz=timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
