from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backtesting.engine import BacktestConfig, BacktestEngine
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
from tmp_fase56_h27_reconstruction import CAPITAL, LAB_PARAMS, load_candles


BASE = Path("optimization/results")
STATUS_PATH = BASE / "fase56_v1_diagnostic_status.json"
RESULT_PATH = BASE / "fase56_v1_diagnostic_result.json"

QUIET_LOGGERS = [
    "risk.risk_manager",
    "risk.position_sizer",
    "database.connection",
]


def configure_logging() -> None:
    for logger_name in QUIET_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def write_status(stage: str, started_at: float, extra: dict | None = None) -> None:
    payload = {
        "stage": stage,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }
    if extra:
        payload.update(extra)
    STATUS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def main() -> None:
    configure_logging()
    BASE.mkdir(parents=True, exist_ok=True)
    started_at = time.perf_counter()

    write_status("starting", started_at)

    candles = load_candles()
    write_status("candles_loaded", started_at, {"bars": len(candles)})

    strategy = ReversaoNextGenV1Strategy(**LAB_PARAMS)
    strategy.initialize()
    write_status("strategy_initialized", started_at)

    enriched = strategy.calculate(candles.copy())
    write_status(
        "indicators_calculated",
        started_at,
        {
            "bars": len(enriched),
            "columns": len(enriched.columns),
            "last_index": str(enriched.index[-1]) if len(enriched) else None,
        },
    )

    sample_window = enriched.iloc[: max(60, min(len(enriched), 500))].copy()
    sample_signal = strategy.entry_signal(sample_window)
    write_status(
        "entry_signal_checked",
        started_at,
        {
            "sample_signal": str(sample_signal.signal),
            "sample_score": float(sample_signal.score),
        },
    )

    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))
    write_status("engine_created", started_at)

    result = engine.run(candles.copy(), symbol="BTC/USDT")
    write_status(
        "engine_completed",
        started_at,
        {
            "trades": len(result.trades),
            "net_profit": float(result.metrics.net_profit),
            "profit_factor": float(result.metrics.profit_factor),
            "sharpe_ratio": float(result.metrics.sharpe_ratio),
        },
    )

    RESULT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "strategy": result.strategy_name,
                "metrics": asdict(result.metrics),
                "trades": len(result.trades),
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    write_status("completed", started_at, {"result_path": str(RESULT_PATH)})


if __name__ == "__main__":
    main()
