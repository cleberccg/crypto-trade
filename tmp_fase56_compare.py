from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, wait
import logging
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import time

from backtesting.engine import BacktestConfig, BacktestEngine
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
from strategies.reversao_nextgen_v2 import ReversaoNextGenV2Strategy
from tmp_fase56_h27_reconstruction import CAPITAL, LAB_PARAMS, load_candles


BASE = Path("optimization/results")
STATUS_PATH = BASE / "fase56_v1_v2_status.json"

QUIET_LOGGERS = [
    "strategies.reversao_nextgen_v1",
    "strategies.reversao_nextgen_v2",
    "backtesting.engine",
    "risk.risk_manager",
    "risk.position_sizer",
    "database.connection",
]


def configure_quiet_logging() -> None:
    for logger_name in QUIET_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def run_backtest(strategy):
    strategy.initialize()
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))
    result = engine.run(load_candles(), symbol="BTC/USDT")
    return result


def run_backtest_summary(strategy_name: str) -> dict:
    configure_quiet_logging()

    if strategy_name == "v1":
        strategy = ReversaoNextGenV1Strategy(**LAB_PARAMS)
    elif strategy_name == "v2":
        strategy = ReversaoNextGenV2Strategy(**LAB_PARAMS)
    else:
        raise ValueError(f"Unknown strategy name: {strategy_name}")

    result = run_backtest(strategy)
    return {
        "strategy": result.strategy_name,
        "metrics": asdict(result.metrics),
        "trades": len(result.trades),
    }


def main() -> None:
    configure_quiet_logging()
    BASE.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(
            {
                "state": "started",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "done": [],
                "pending": ["v1", "v2"],
                "elapsed_seconds": 0,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    with ProcessPoolExecutor(max_workers=2) as executor:
        v1_future = executor.submit(run_backtest_summary, "v1")
        v2_future = executor.submit(run_backtest_summary, "v2")
        futures = {
            "v1": v1_future,
            "v2": v2_future,
        }
        started_at = time.perf_counter()

        while True:
            done, not_done = wait(futures.values(), timeout=15)
            elapsed = int(time.perf_counter() - started_at)
            done_labels = [label for label, future in futures.items() if future.done()]
            pending_labels = [label for label, future in futures.items() if not future.done()]

            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "done": done_labels,
                        "pending": pending_labels,
                        "elapsed_seconds": elapsed,
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )

            print(
                f"HEARTBEAT elapsed={elapsed}s done={done_labels or ['none']} pending={pending_labels or ['none']}",
                flush=True,
            )

            if not not_done:
                break

        v1_summary = v1_future.result()
        v2_summary = v2_future.result()

    comparison = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "BTC/USDT",
        "timeframe": "5m",
        "v1": v1_summary,
        "v2": v2_summary,
    }

    json_path = BASE / "fase56_v1_v2_comparison.json"
    md_path = BASE / "fase56_v1_v2_comparison.md"
    json_path.write_text(json.dumps(comparison, indent=2, ensure_ascii=True), encoding="utf-8")
    STATUS_PATH.write_text(
        json.dumps(
            {
                "state": "completed",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "done": ["v1", "v2"],
                "pending": [],
                "elapsed_seconds": None,
                "comparison_json": str(json_path),
                "comparison_md": str(md_path),
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    lines = [
        "# FASE 5.6 - Comparativo V1 x V2",
        "",
        f"- V1 trades: {comparison['v1']['trades']}",
        f"- V1 win rate: {comparison['v1']['metrics']['win_rate']:.4f}",
        f"- V1 net profit: {comparison['v1']['metrics']['net_profit']:.2f}",
        f"- V1 profit factor: {comparison['v1']['metrics']['profit_factor']:.4f}",
        f"- V1 sharpe: {comparison['v1']['metrics']['sharpe_ratio']:.4f}",
        f"- V1 expectancy: {comparison['v1']['metrics']['expectancy']:.4f}",
        f"- V1 max drawdown pct: {comparison['v1']['metrics']['max_drawdown_pct']:.4f}",
        "",
        f"- V2 trades: {comparison['v2']['trades']}",
        f"- V2 win rate: {comparison['v2']['metrics']['win_rate']:.4f}",
        f"- V2 net profit: {comparison['v2']['metrics']['net_profit']:.2f}",
        f"- V2 profit factor: {comparison['v2']['metrics']['profit_factor']:.4f}",
        f"- V2 sharpe: {comparison['v2']['metrics']['sharpe_ratio']:.4f}",
        f"- V2 expectancy: {comparison['v2']['metrics']['expectancy']:.4f}",
        f"- V2 max drawdown pct: {comparison['v2']['metrics']['max_drawdown_pct']:.4f}",
        "",
        "## Summary",
    ]

    if comparison["v2"]["metrics"]["profit_factor"] > comparison["v1"]["metrics"]["profit_factor"] and comparison["v2"]["metrics"]["sharpe_ratio"] > comparison["v1"]["metrics"]["sharpe_ratio"]:
        lines.append("- V2 improved over V1 on profit factor and Sharpe.")
    else:
        lines.append("- V2 did not improve over V1 on both profit factor and Sharpe.")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print("WROTE", json_path)
    print("WROTE", md_path)
    print("WROTE", STATUS_PATH)


if __name__ == "__main__":
    main()
