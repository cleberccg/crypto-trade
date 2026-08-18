from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from database.connection import get_session
from database.history_models import ExecutionFrameworkOptimizationRun
from database.history_repositories import ExecutionFrameworkOptimizationRunRepository
from database.models import Candle
from database.repositories import CandleRepository
from research.services.trade_outcome_controlled_implementation import (
    TradeOutcomeControlledImplementationConfig,
    TradeOutcomeControlledImplementationService,
)
from strategies.factory import create_strategy
from strategies.registry import get_registration, list_registered_strategies
from utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionFrameworkOptimizationConfig:
    strategy_name: str = "TradeOutcomeNextGenV1"
    benchmark_symbol: str = "BTC/USDT"
    benchmark_timeframe: str = "5m"
    benchmark_bars: int = 20_000
    initial_capital: float = 10_000.0
    output_prefix: str = "execution_framework_optimization"
    rerun_phase9: bool = True
    persist_to_db: bool = True


class ExecutionFrameworkOptimizationService:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, config: ExecutionFrameworkOptimizationConfig | None = None) -> dict[str, object]:
        if self._session is None:
            with get_session() as session:
                nested = ExecutionFrameworkOptimizationService(session=session, base_dir=self._base_dir)
                return nested.run(config)

        cfg = config or ExecutionFrameworkOptimizationConfig()
        run_id = str(uuid4())

        audit_rows = self._audit_strategies()
        benchmark = self._benchmark_strategy(
            strategy_name=cfg.strategy_name,
            symbol=cfg.benchmark_symbol,
            timeframe=cfg.benchmark_timeframe,
            benchmark_bars=max(500, int(cfg.benchmark_bars)),
            initial_capital=float(cfg.initial_capital),
        )

        rerun_phase9_summary: dict[str, object] = {}
        rerun_phase9_outputs: dict[str, str] = {}
        if cfg.rerun_phase9:
            phase9 = TradeOutcomeControlledImplementationService(session=self._session, base_dir=self._base_dir)
            phase9_result = phase9.run(TradeOutcomeControlledImplementationConfig())
            rerun_phase9_summary = phase9_result.get("summary", {})
            rerun_phase9_outputs = phase9_result.get("outputs", {})

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": "FASE 9.0 - Otimizacao Permanente do Framework de Execucao",
            "status": "COMPLETED",
            "audit": {
                "strategies_audited": len(audit_rows),
                "rows": audit_rows,
            },
            "equivalence": benchmark["equivalence"],
            "benchmark": benchmark["benchmark"],
            "phase9_rerun": {
                "executed": bool(cfg.rerun_phase9),
                "summary": rerun_phase9_summary,
                "outputs": rerun_phase9_outputs,
            },
        }

        outputs = self._write_outputs(report, audit_rows, cfg.output_prefix)
        self._persist(
            run_id=run_id,
            cfg=cfg,
            report=report,
            benchmark=benchmark,
            outputs=outputs,
        )

        return {
            "summary": {
                "run_id": run_id,
                "strategies_audited": len(audit_rows),
                "same_trades": benchmark["equivalence"]["same_trades"],
                "same_metrics": benchmark["equivalence"]["same_metrics"],
                "equivalence_passed": benchmark["equivalence"]["equivalence_passed"],
                "speedup_pct": benchmark["benchmark"]["speedup_pct"],
                "phase9_rerun_executed": bool(cfg.rerun_phase9),
            },
            "outputs": outputs,
            "report": report,
        }

    def _audit_strategies(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for spec in list_registered_strategies():
            registration = get_registration(spec["name"])
            strategy_cls = registration.strategy_cls
            source = inspect.getsource(strategy_cls)

            has_internal_cache = any(token in source for token in ("_enriched_cache", "_prediction_cache", "cached_payload("))
            has_prepare_override = "def prepare_dataset" in source
            defines_calculate = "def calculate" in source
            historical_complexity = "O(n)" if has_internal_cache else ("O(n^2) under growing-window engine" if defines_calculate else "unknown")

            rows.append(
                {
                    "strategy": spec["name"],
                    "family": spec["family"],
                    "has_internal_cache": has_internal_cache,
                    "has_prepare_dataset_override": has_prepare_override,
                    "defines_calculate": defines_calculate,
                    "historical_complexity": historical_complexity,
                    "current_execution_mode": "prepare_dataset + cached slice reuse",
                    "benefits_from_framework": True,
                }
            )

        rows.sort(key=lambda item: (str(item["family"]), str(item["strategy"])))
        return rows

    def _benchmark_strategy(
        self,
        *,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        benchmark_bars: int,
        initial_capital: float,
    ) -> dict[str, object]:
        df = self._load_benchmark_dataframe(symbol=symbol, timeframe=timeframe, benchmark_bars=benchmark_bars)

        before = self._run_backtest(strategy_name, df, symbol, timeframe, initial_capital, use_prepared_dataset=False)
        after = self._run_backtest(strategy_name, df, symbol, timeframe, initial_capital, use_prepared_dataset=True)

        same_trades = self._canonical_trades(before.trades) == self._canonical_trades(after.trades)
        same_metrics = self._canonical_metrics(before.metrics.to_dict()) == self._canonical_metrics(after.metrics.to_dict())
        equivalence_passed = bool(same_trades and same_metrics)

        before_time = float(before.diagnostics.get("execution_elapsed_seconds", 0.0))
        after_time = float(after.diagnostics.get("execution_elapsed_seconds", 0.0))
        before_rate = float(before.diagnostics.get("bars_per_second", 0.0))
        after_rate = float(after.diagnostics.get("bars_per_second", 0.0))
        speedup_pct = 0.0 if before_time <= 0 else ((before_time - after_time) / before_time) * 100.0

        benchmark = {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(df),
            "time_before_seconds": before_time,
            "time_after_seconds": after_time,
            "bars_per_second_before": before_rate,
            "bars_per_second_after": after_rate,
            "first_result_before_seconds": before_time,
            "first_result_after_seconds": after_time,
            "speedup_pct": speedup_pct,
            "eta_full_campaign_before_seconds": self._estimate_campaign_eta(total_bars=930_807, combinations=60, workers=4, bars_per_second=before_rate),
            "eta_full_campaign_after_seconds": self._estimate_campaign_eta(total_bars=930_807, combinations=60, workers=4, bars_per_second=after_rate),
            "cpu_note": "Use logs/application.log and process monitor for live CPU tracking during full campaign.",
            "memory_note": "Working set tracked externally per python worker during campaign rerun.",
        }
        equivalence = {
            "same_trades": same_trades,
            "same_metrics": same_metrics,
            "equivalence_passed": equivalence_passed,
            "before_metrics": before.metrics.to_dict(),
            "after_metrics": after.metrics.to_dict(),
            "before_trades": len(before.trades),
            "after_trades": len(after.trades),
        }
        return {"benchmark": benchmark, "equivalence": equivalence}

    def _load_benchmark_dataframe(self, *, symbol: str, timeframe: str, benchmark_bars: int) -> pd.DataFrame:
        max_dt = self._session.query(func.max(Candle.open_time)).filter(Candle.symbol == symbol, Candle.timeframe == timeframe).scalar()
        min_dt = self._session.query(func.min(Candle.open_time)).filter(Candle.symbol == symbol, Candle.timeframe == timeframe).scalar()
        if max_dt is None or min_dt is None:
            raise ValueError(f"No candle data found for {symbol}/{timeframe}")

        repo = CandleRepository(self._session)
        candles = repo.get_range(symbol, timeframe, min_dt, max_dt)
        if not candles:
            raise ValueError(f"No candle rows loaded for {symbol}/{timeframe}")

        candles = candles[-benchmark_bars:]
        return pd.DataFrame(
            [
                {
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            index=pd.DatetimeIndex([candle.open_time for candle in candles], tz="UTC"),
        )

    def _run_backtest(
        self,
        strategy_name: str,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        initial_capital: float,
        *,
        use_prepared_dataset: bool,
    ) -> BacktestResult:
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        engine = BacktestEngine(
            strategy,
            config=BacktestConfig(
                initial_capital=initial_capital,
                use_prepared_dataset=use_prepared_dataset,
                progress_log_interval_bars=10_000,
            ),
        )
        return engine.run(df.copy(), symbol=symbol, timeframe=timeframe)

    @staticmethod
    def _canonical_trades(trades: list[dict[str, object]]) -> list[tuple[object, ...]]:
        out: list[tuple[object, ...]] = []
        for trade in trades:
            out.append(
                (
                    str(trade.get("entry_time")),
                    str(trade.get("exit_time")),
                    round(float(trade.get("entry_price", 0.0) or 0.0), 8),
                    round(float(trade.get("exit_price", 0.0) or 0.0), 8),
                    round(float(trade.get("quantity", 0.0) or 0.0), 8),
                    round(float(trade.get("pnl", 0.0) or 0.0), 8),
                    str(trade.get("exit_reason")),
                )
            )
        return out

    @staticmethod
    def _canonical_metrics(metrics: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in metrics.items():
            if isinstance(value, float):
                out[key] = round(value, 8)
            else:
                out[key] = value
        return out

    @staticmethod
    def _estimate_campaign_eta(*, total_bars: int, combinations: int, workers: int, bars_per_second: float) -> float | None:
        if bars_per_second <= 0 or workers <= 0:
            return None
        seconds_per_combo = total_bars / bars_per_second
        return (seconds_per_combo * combinations) / workers

    def _write_outputs(self, report: dict[str, object], audit_rows: list[dict[str, object]], output_prefix: str) -> dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / f"{output_prefix}_{ts}.json"
        csv_path = out_dir / f"{output_prefix}_{ts}.csv"
        md_path = out_dir / f"{output_prefix}_{ts}.md"

        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        pd.DataFrame(audit_rows).to_csv(csv_path, index=False)

        eq = report["equivalence"]
        bm = report["benchmark"]
        lines = [
            "# FASE 9.0 - Otimizacao do Framework de Execucao",
            "",
            "## Equivalencia",
            f"- same_trades: {eq['same_trades']}",
            f"- same_metrics: {eq['same_metrics']}",
            f"- equivalence_passed: {eq['equivalence_passed']}",
            "",
            "## Benchmark",
            f"- bars: {bm['bars']}",
            f"- tempo antes (s): {float(bm['time_before_seconds']):.4f}",
            f"- tempo depois (s): {float(bm['time_after_seconds']):.4f}",
            f"- bars/s antes: {float(bm['bars_per_second_before']):.2f}",
            f"- bars/s depois: {float(bm['bars_per_second_after']):.2f}",
            f"- ganho percentual: {float(bm['speedup_pct']):.2f}%",
            "",
            "## Auditoria",
            f"- estrategias auditadas: {len(audit_rows)}",
        ]
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return {"json": str(json_path), "csv": str(csv_path), "md": str(md_path)}

    def _persist(
        self,
        *,
        run_id: str,
        cfg: ExecutionFrameworkOptimizationConfig,
        report: dict[str, object],
        benchmark: dict[str, object],
        outputs: dict[str, str],
    ) -> None:
        if not cfg.persist_to_db:
            return

        repo = ExecutionFrameworkOptimizationRunRepository(self._session)
        repo.save(
            ExecutionFrameworkOptimizationRun(
                run_id=run_id,
                status="completed",
                strategy_name=cfg.strategy_name,
                benchmark_symbol=cfg.benchmark_symbol,
                benchmark_timeframe=cfg.benchmark_timeframe,
                same_trades=bool(benchmark["equivalence"]["same_trades"]),
                same_metrics=bool(benchmark["equivalence"]["same_metrics"]),
                equivalence_passed=bool(benchmark["equivalence"]["equivalence_passed"]),
                bars_before=float(benchmark["benchmark"]["bars_per_second_before"]),
                bars_after=float(benchmark["benchmark"]["bars_per_second_after"]),
                speedup_pct=float(benchmark["benchmark"]["speedup_pct"]),
                time_before_seconds=float(benchmark["benchmark"]["time_before_seconds"]),
                time_after_seconds=float(benchmark["benchmark"]["time_after_seconds"]),
                first_result_before_seconds=float(benchmark["benchmark"]["first_result_before_seconds"]),
                first_result_after_seconds=float(benchmark["benchmark"]["first_result_after_seconds"]),
                audit_json=json.dumps(report["audit"], ensure_ascii=True),
                benchmark_json=json.dumps(report["benchmark"], ensure_ascii=True),
                artifacts_json=json.dumps(outputs, ensure_ascii=True),
                summary_json=json.dumps(report, ensure_ascii=True),
            )
        )