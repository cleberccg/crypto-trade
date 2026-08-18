from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from strategies.base_strategy import SignalType, StrategySignal
from strategies.reversao_nextgen_v2 import ReversaoNextGenV2Strategy
from tmp_fase56_h27_reconstruction import CAPITAL, LAB_PARAMS, load_candles


BASE = Path("optimization/results")
OUT_JSON = BASE / "fase56_v2_equivalence_benchmark.json"
OUT_MD = BASE / "fase56_v2_equivalence_benchmark.md"
STATUS_JSON = BASE / "fase56_v2_equivalence_status.json"


def write_status(state: str, progress_pct: float, stage: str, extra: dict | None = None) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "stage": stage,
        "progress_pct": round(float(max(0.0, min(100.0, progress_pct))), 2),
    }
    if extra:
        payload.update(extra)
    STATUS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def estimate_expected_seconds(candles_count: int, stage: str) -> float:
    # Use latest benchmark as seed for ETA; fallback to conservative defaults.
    default_bps = 70.0 if stage == "legacy" else 65.0
    if OUT_JSON.exists():
        try:
            data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            bps = float(data.get("benchmark", {}).get(stage, {}).get("bars_per_second", default_bps))
            if bps > 0:
                default_bps = bps
        except Exception:
            pass
    return max(1.0, candles_count / default_bps)


def start_stage_heartbeat(
    stage: str,
    stage_start_pct: float,
    stage_end_pct: float,
    expected_seconds: float,
    stop_event: threading.Event,
    started_at: float,
) -> threading.Thread:
    def _run() -> None:
        while not stop_event.is_set():
            elapsed = time.perf_counter() - started_at
            ratio = min(1.0, elapsed / expected_seconds) if expected_seconds > 0 else 0.0
            progress = stage_start_pct + (stage_end_pct - stage_start_pct) * ratio
            write_status(
                "running",
                progress,
                stage,
                {
                    "elapsed_seconds": round(elapsed, 2),
                    "expected_seconds": round(expected_seconds, 2),
                },
            )
            stop_event.wait(10)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


class ReversaoNextGenV2Legacy(ReversaoNextGenV2Strategy):
    """Reference V2 behavior: model inference executed per bar in entry/exit."""

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        # Keep indicator pipeline only; no cached model inference.
        return super(ReversaoNextGenV2Strategy, self).calculate(df)

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        self._assert_initialized()
        self._assert_model_loaded()

        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        feature_frame = self._build_feature_frame(last, timestamp)
        prediction = int(self._rule_model.predict(feature_frame)[0])
        probability = float(self._rule_model.predict_proba(feature_frame)[0][1])

        signal = SignalType.BUY if prediction == 1 else SignalType.HOLD
        if signal == SignalType.BUY:
            stop_loss = price - (self._atr_stop_multiplier * atr)
            risk = price - stop_loss
            reward = risk * self._risk_reward_ratio
            take_profit = price + reward
        else:
            stop_loss = None
            take_profit = None

        metadata = {
            "rule_prediction": prediction,
            "rule_probability": probability,
            "atr": atr,
            "reason": "h27_rule_model" if signal == SignalType.BUY else "no_signal",
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=probability,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        self._assert_initialized()
        self._assert_model_loaded()

        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        feature_frame = self._build_feature_frame(last, timestamp)
        prediction = int(self._rule_model.predict(feature_frame)[0])
        probability = float(self._rule_model.predict_proba(feature_frame)[0][1])

        signal = SignalType.SELL if prediction == 0 else SignalType.HOLD
        metadata = {
            "reason": "rule_invalidated" if signal == SignalType.SELL else "rule_held",
            "rule_prediction": prediction,
            "rule_probability": probability,
            "trend_score": float(last.get("trend_score", 0.0)),
            "price": price,
            "entry_price": entry_price,
            "pnl": price - entry_price,
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=probability if signal == SignalType.SELL else 0.0,
            metadata=metadata,
        )


def run_backtest(strategy, candles: pd.DataFrame) -> tuple[dict, float, float]:
    strategy.initialize()
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))

    bars = max(1, len(candles) - engine._config.warmup_bars)
    t0 = time.perf_counter()
    result = engine.run(candles.copy(), symbol="BTC/USDT")
    elapsed = time.perf_counter() - t0
    bars_per_sec = bars / elapsed if elapsed > 0 else 0.0

    return {
        "strategy": result.strategy_name,
        "metrics": asdict(result.metrics),
        "trades": result.trades,
    }, elapsed, bars_per_sec


def compare_trades(reference: list[dict], candidate: list[dict]) -> dict:
    if len(reference) != len(candidate):
        return {
            "equal": False,
            "reason": "trade_count_mismatch",
            "reference_count": len(reference),
            "candidate_count": len(candidate),
        }

    keys = [
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "quantity",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "entry_bar",
        "exit_bar",
    ]

    mismatches = []
    for i, (r, c) in enumerate(zip(reference, candidate)):
        row_diff = {}
        for key in keys:
            rv = r.get(key)
            cv = c.get(key)
            if isinstance(rv, float) or isinstance(cv, float):
                if abs(float(rv) - float(cv)) > 1e-12:
                    row_diff[key] = {"reference": rv, "candidate": cv}
            else:
                if rv != cv:
                    row_diff[key] = {"reference": rv, "candidate": cv}
        if row_diff:
            mismatches.append({"trade_index": i, "diff": row_diff})
            if len(mismatches) >= 5:
                break

    return {
        "equal": len(mismatches) == 0,
        "mismatches": mismatches,
    }


def compare_metrics(reference: dict, candidate: dict) -> dict:
    diffs = {}
    for key, rv in reference.items():
        cv = candidate.get(key)
        if isinstance(rv, float) or isinstance(cv, float):
            if abs(float(rv) - float(cv)) > 1e-12:
                diffs[key] = {"reference": rv, "candidate": cv}
        else:
            if rv != cv:
                diffs[key] = {"reference": rv, "candidate": cv}
    return {"equal": len(diffs) == 0, "diffs": diffs}


def main() -> None:
    parser = argparse.ArgumentParser(description="V2 equivalence and performance benchmark")
    parser.add_argument("--limit", type=int, default=0, help="Optional candle limit for quick runtime estimation")
    args = parser.parse_args()

    logging.disable(logging.CRITICAL)

    BASE.mkdir(parents=True, exist_ok=True)
    candles = load_candles()
    total_candles = len(candles)
    if args.limit and args.limit > 0:
        candles = candles.iloc[: args.limit].copy()

    write_status("started", 0.0, "initializing", {"candles": len(candles), "limit": args.limit})

    print(f"[benchmark] candles={len(candles)} total_available={total_candles}", flush=True)
    print("[benchmark] running legacy...", flush=True)

    legacy_expected = estimate_expected_seconds(len(candles), "legacy")
    legacy_stop = threading.Event()
    legacy_thread = start_stage_heartbeat(
        stage="legacy",
        stage_start_pct=0.0,
        stage_end_pct=50.0,
        expected_seconds=legacy_expected,
        stop_event=legacy_stop,
        started_at=time.perf_counter(),
    )

    legacy_data, legacy_elapsed, legacy_bps = run_backtest(ReversaoNextGenV2Legacy(**LAB_PARAMS), candles)
    legacy_stop.set()
    legacy_thread.join(timeout=1)
    write_status(
        "running",
        50.0,
        "legacy_completed",
        {"legacy_elapsed_seconds": round(legacy_elapsed, 3), "legacy_bars_per_second": round(legacy_bps, 2)},
    )

    print(f"[benchmark] legacy done in {legacy_elapsed:.3f}s ({legacy_bps:.2f} bars/s)", flush=True)
    print("[benchmark] running optimized...", flush=True)

    optimized_expected = estimate_expected_seconds(len(candles), "optimized")
    optimized_stop = threading.Event()
    optimized_thread = start_stage_heartbeat(
        stage="optimized",
        stage_start_pct=50.0,
        stage_end_pct=99.0,
        expected_seconds=optimized_expected,
        stop_event=optimized_stop,
        started_at=time.perf_counter(),
    )

    optimized_data, optimized_elapsed, optimized_bps = run_backtest(ReversaoNextGenV2Strategy(**LAB_PARAMS), candles)
    optimized_stop.set()
    optimized_thread.join(timeout=1)
    write_status(
        "running",
        99.0,
        "optimized_completed",
        {
            "optimized_elapsed_seconds": round(optimized_elapsed, 3),
            "optimized_bars_per_second": round(optimized_bps, 2),
        },
    )

    print(f"[benchmark] optimized done in {optimized_elapsed:.3f}s ({optimized_bps:.2f} bars/s)", flush=True)

    trades_cmp = compare_trades(legacy_data["trades"], optimized_data["trades"])
    metrics_cmp = compare_metrics(legacy_data["metrics"], optimized_data["metrics"])

    equivalent = trades_cmp["equal"] and metrics_cmp["equal"]

    speedup_pct = ((legacy_elapsed - optimized_elapsed) / legacy_elapsed * 100.0) if legacy_elapsed > 0 else 0.0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "equivalent": equivalent,
        "trades_comparison": trades_cmp,
        "metrics_comparison": metrics_cmp,
        "benchmark": {
            "legacy": {
                "elapsed_seconds": legacy_elapsed,
                "bars_per_second": legacy_bps,
                "trades": len(legacy_data["trades"]),
            },
            "optimized": {
                "elapsed_seconds": optimized_elapsed,
                "bars_per_second": optimized_bps,
                "trades": len(optimized_data["trades"]),
            },
            "speedup_percent": speedup_pct,
        },
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    lines = [
        "# FASE 5.6 - Equivalencia V2 (Legado vs Otimizada)",
        "",
        f"- Equivalent: {equivalent}",
        f"- Legacy elapsed (s): {legacy_elapsed:.3f}",
        f"- Optimized elapsed (s): {optimized_elapsed:.3f}",
        f"- Legacy bars/s: {legacy_bps:.2f}",
        f"- Optimized bars/s: {optimized_bps:.2f}",
        f"- Speedup (%): {speedup_pct:.2f}",
        f"- Legacy trades: {len(legacy_data['trades'])}",
        f"- Optimized trades: {len(optimized_data['trades'])}",
    ]

    if not equivalent:
        lines.append("")
        lines.append("## Divergence")
        if not trades_cmp["equal"]:
            lines.append(f"- Trades mismatch: {json.dumps(trades_cmp, ensure_ascii=True)}")
        if not metrics_cmp["equal"]:
            lines.append(f"- Metrics mismatch: {json.dumps(metrics_cmp, ensure_ascii=True)}")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    write_status(
        "completed",
        100.0,
        "completed",
        {
            "equivalent": equivalent,
            "benchmark_json": str(OUT_JSON),
            "benchmark_md": str(OUT_MD),
        },
    )

    print(f"[benchmark] equivalent={equivalent}", flush=True)
    print("WROTE", OUT_JSON)
    print("WROTE", OUT_MD)


if __name__ == "__main__":
    main()
