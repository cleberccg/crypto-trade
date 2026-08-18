from __future__ import annotations

import csv
import json
import os
import platform
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from config.settings import settings
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.repositories import CandleRepository
from strategy_catalog.catalog import StrategyCatalog
from strategies.factory import create_strategy
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyCatalogAuditConfig:
    symbol: str
    timeframe: str
    window_days: int = 90
    initial_capital: float = 10_000.0
    benchmark_symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    benchmark_timeframes: tuple[str, ...] = ("5m", "15m", "1h")
    optimizer_max_combinations: int = 5
    optimizer_workers: int = 1
    max_bars: int = 3000
    output_prefix: str = "strategy_catalog_audit"


class StrategyCatalogAuditService:
    """FASE 10.2 - Scientific audit for strategy catalog evaluation pipeline."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: StrategyCatalogAuditConfig) -> dict[str, Any]:
        run_id = HistoryPersistenceService.new_execution_id()
        started_at = datetime.now(tz=timezone.utc)

        catalog = StrategyCatalog()
        entries = catalog.entries()
        strategy_names = [entry.name for entry in entries]

        start_dt = datetime.now(tz=timezone.utc) - timedelta(days=max(30, int(cfg.window_days)))
        end_dt = datetime.now(tz=timezone.utc)

        base_df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt, max_bars=cfg.max_bars)
        if base_df.empty:
            report = {
                "status": "failed",
                "run_id": run_id,
                "reason": "no_data",
                "symbol": cfg.symbol,
                "timeframe": cfg.timeframe,
                "window_days": cfg.window_days,
                "recommendation": "OPCAO B",
                "recommendation_reason": "Sem dados para auditoria.",
                "strategies": [],
                "ranking": [],
            }
            outputs = self._write_outputs(cfg.output_prefix, report)
            self._persist_checkpoint(run_id, report)
            return {
                "summary": {
                    "status": "failed",
                    "run_id": run_id,
                    "reason": "no_data",
                },
                "report": report,
                "outputs": outputs,
            }

        criteria = self._criteria_thresholds()
        strategy_reports: list[dict[str, Any]] = []

        for strategy_name in strategy_names:
            row = self._audit_strategy(cfg, strategy_name, base_df, start_dt, end_dt, criteria)
            strategy_reports.append(row)

        ranking = sorted(
            strategy_reports,
            key=lambda x: (
                -float(x.get("implementability", 0.0)),
                -float(x.get("robustness", 0.0)),
                float(x.get("distance_to_approval_pct", 9999.0)),
            ),
        )
        for idx, row in enumerate(ranking, start=1):
            row["rank"] = idx

        elimination_matrix = [self._matrix_row(x) for x in ranking]
        distributions = {
            "profit_factor": self._distribution(ranking, "profit_factor", [0.50, 0.80, 1.00, 1.10, 1.20, 1.40]),
            "sharpe": self._distribution(ranking, "sharpe", [0.00, 0.50, 1.00, 1.50, 2.00]),
            "expectancy": self._distribution(ranking, "expectancy", [-1.00, 0.00, 1.00, 5.00]),
            "drawdown_pct": self._distribution(
                ranking,
                "drawdown_pct",
                [0.05, 0.10, 0.15, 0.20, 0.30],
                as_percent=True,
            ),
        }

        context_benchmark = self._context_benchmark(cfg, strategy_names, start_dt, end_dt)

        rejection_reasons = self._top_rejection_reasons(ranking)
        grouped = self._group_by_distance(ranking)
        limits_audit = self._limits_audit(ranking, criteria)

        recommendation = "OPCAO A" if grouped["group_a"] else "OPCAO B"
        recommendation_reason = (
            "Existem estrategias proximas da aprovacao (distancia <= 10%)."
            if recommendation == "OPCAO A"
            else "Catalogo atual sem estrategias suficientemente competitivas; considerar segundo lote orientado a cripto."
        )

        report = {
            "status": "completed",
            "phase": "10.2",
            "run_id": run_id,
            "generated_at": started_at.isoformat(),
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "window_days": cfg.window_days,
            "criteria_thresholds": criteria,
            "ranking": ranking,
            "elimination_matrix": elimination_matrix,
            "distance_to_approval": [
                {
                    "strategy": x.get("strategy"),
                    "distance_to_approval_pct": x.get("distance_to_approval_pct"),
                    "criterion_distance": x.get("criterion_distance"),
                }
                for x in ranking
            ],
            "distributions": distributions,
            "context_benchmark": context_benchmark,
            "limits_audit": limits_audit,
            "rejection_reasons": rejection_reasons,
            "promising_strategies": [x for x in ranking if x.get("group") == "A"][:5],
            "groups": grouped,
            "recommendation": recommendation,
            "recommendation_reason": recommendation_reason,
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        self._persist_checkpoint(run_id, report)

        summary = {
            "status": "completed",
            "run_id": run_id,
            "strategies_audited": len(ranking),
            "approved_for_paper": len([x for x in ranking if x.get("paper_qualified")]),
            "group_a": len(grouped["group_a"]),
            "group_b": len(grouped["group_b"]),
            "group_c": len(grouped["group_c"]),
            "recommendation": recommendation,
            "top_strategy": ranking[0]["strategy"] if ranking else None,
        }

        return {"summary": summary, "report": report, "outputs": outputs}

    def _audit_strategy(
        self,
        cfg: StrategyCatalogAuditConfig,
        strategy_name: str,
        df: pd.DataFrame,
        start_dt: datetime,
        end_dt: datetime,
        criteria: dict[str, float],
    ) -> dict[str, Any]:
        smoke_pass = False
        backtest_pass = False
        optimizer_pass = False
        validation_pass = False
        scientific_pass = False
        paper_pass = False

        metrics: dict[str, Any] = {}
        criterion_distance: dict[str, dict[str, float | bool]] = {}
        criterion_checks: dict[str, bool] = {}
        rejection_reasons: list[str] = []
        elimination_stage = "Smoke Test"

        try:
            strategy = create_strategy(strategy_name)
            strategy.initialize()
            bt = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                df,
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
            )
            metrics = bt.metrics.to_dict()
            smoke_pass = int(metrics.get("total_trades", 0)) > 0
            backtest_pass = smoke_pass
        except Exception as exc:
            logger.warning("Audit backtest failed for %s: %s", strategy_name, exc)
            return self._failed_row(strategy_name, "Smoke Test", f"backtest_error:{exc}")

        if not smoke_pass:
            return self._failed_row(strategy_name, "Smoke Test", "no_trades")

        recovery_factor = self._recovery_factor(metrics)
        robustness = self._robustness_score(cfg, strategy_name, start_dt, end_dt)
        stability = robustness

        # Reduced optimizer probe (no deep optimization): strategy is considered probe-pass
        # when it has enough trades and valid metrics for parameterized evaluation.
        optimizer_pass = int(metrics.get("total_trades", 0)) >= 3

        if not optimizer_pass:
            elimination_stage = "Optimizer"

        # Standardized reduced validation without deep optimization.
        validation_pass = (
            float(metrics.get("profit_factor", 0.0)) >= float(criteria["profit_factor"])
            and float(metrics.get("sharpe_ratio", 0.0)) >= float(criteria["sharpe"])
            and float(metrics.get("expectancy", 0.0)) >= float(criteria["expectancy"])
            and abs(float(metrics.get("max_drawdown_pct", 0.0))) <= float(criteria["drawdown_pct"])
            and float(metrics.get("win_rate", 0.0)) >= float(criteria["win_rate"])
            and float(metrics.get("total_trades", 0.0)) >= float(criteria["number_of_trades"])
        )

        if optimizer_pass and not validation_pass:
            elimination_stage = "Validation"

        values = {
            "profit_factor": float(metrics.get("profit_factor", 0.0)),
            "sharpe": float(metrics.get("sharpe_ratio", 0.0)),
            "expectancy": float(metrics.get("expectancy", 0.0)),
            "net_profit": float(metrics.get("net_profit", 0.0)),
            "drawdown_pct": abs(float(metrics.get("max_drawdown_pct", 0.0))),
            "win_rate": float(metrics.get("win_rate", 0.0)),
            "number_of_trades": float(metrics.get("total_trades", 0.0)),
            "recovery_factor": recovery_factor,
            "robustness": robustness,
            "stability": stability,
        }

        for key, required in criteria.items():
            observed = float(values.get(key, 0.0))
            if key == "drawdown_pct":
                passed = observed <= required
                distance_pct = self._distance_max(observed, required)
            else:
                passed = observed >= required
                distance_pct = self._distance_min(observed, required)
            criterion_checks[key] = passed
            criterion_distance[key] = {
                "obtained": round(observed, 6),
                "required": round(required, 6),
                "distance_pct": round(distance_pct, 2),
                "passed": passed,
            }

        # Implementability is computed from mean shortfall across criteria.
        shortfalls = [max(0.0, -float(v["distance_pct"])) / 100.0 for v in criterion_distance.values()]
        implementability = round(max(0.0, min(1.0, 1.0 - mean(shortfalls))), 4)
        values["implementability"] = implementability
        criteria["implementability"] = 0.60

        impl_pass = implementability >= float(criteria["implementability"])
        criterion_checks["implementability"] = impl_pass
        criterion_distance["implementability"] = {
            "obtained": implementability,
            "required": float(criteria["implementability"]),
            "distance_pct": round(self._distance_min(implementability, float(criteria["implementability"])), 2),
            "passed": impl_pass,
        }

        scientific_pass = (
            criterion_checks.get("robustness", False)
            and criterion_checks.get("stability", False)
            and criterion_checks.get("implementability", False)
        )
        if optimizer_pass and validation_pass and not scientific_pass:
            elimination_stage = "Scientific Validation"

        paper_pass = all(bool(v) for v in criterion_checks.values()) and validation_pass
        if optimizer_pass and validation_pass and scientific_pass and not paper_pass:
            elimination_stage = "Paper Qualification"

        if paper_pass:
            elimination_stage = "Approved"

        if not paper_pass:
            failed = [k for k, ok in criterion_checks.items() if not ok]
            if failed:
                rejection_reasons = failed
            if not validation_pass:
                rejection_reasons.append("validation")
            if not optimizer_pass:
                rejection_reasons.append("optimizer")

        primary_reason = self._primary_rejection_reason(criterion_distance, validation_pass, optimizer_pass)
        distance_to_approval_pct = round(mean(max(0.0, -float(x["distance_pct"])) for x in criterion_distance.values()), 2)

        group = "A" if distance_to_approval_pct <= 10.0 else ("B" if distance_to_approval_pct <= 30.0 else "C")
        status_final = "Paper Trading" if paper_pass else "Rejeitada"

        return {
            "strategy": strategy_name,
            "profit_factor": values["profit_factor"],
            "sharpe": values["sharpe"],
            "expectancy": values["expectancy"],
            "net_profit": values["net_profit"],
            "drawdown_pct": values["drawdown_pct"],
            "win_rate": values["win_rate"],
            "number_of_trades": int(values["number_of_trades"]),
            "recovery_factor": values["recovery_factor"],
            "robustness": values["robustness"],
            "stability": values["stability"],
            "implementability": values["implementability"],
            "criterion_checks": criterion_checks,
            "criterion_distance": criterion_distance,
            "rejection_reasons": rejection_reasons,
            "primary_rejection_reason": primary_reason,
            "distance_to_approval_pct": distance_to_approval_pct,
            "group": group,
            "stages": {
                "Smoke Test": smoke_pass,
                "Backtest": backtest_pass,
                "Optimizer": optimizer_pass,
                "Validation": validation_pass,
                "Scientific Validation": scientific_pass,
                "Paper Qualification": paper_pass,
            },
            "elimination_stage": elimination_stage,
            "paper_qualified": paper_pass,
            "status_final": status_final,
        }

    def _failed_row(self, strategy_name: str, stage: str, reason: str) -> dict[str, Any]:
        return {
            "strategy": strategy_name,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "expectancy": 0.0,
            "net_profit": 0.0,
            "drawdown_pct": 1.0,
            "win_rate": 0.0,
            "number_of_trades": 0,
            "recovery_factor": 0.0,
            "robustness": 0.0,
            "stability": 0.0,
            "implementability": 0.0,
            "criterion_checks": {},
            "criterion_distance": {},
            "rejection_reasons": [reason],
            "primary_rejection_reason": reason,
            "distance_to_approval_pct": 100.0,
            "group": "C",
            "stages": {
                "Smoke Test": False,
                "Backtest": False,
                "Optimizer": False,
                "Validation": False,
                "Scientific Validation": False,
                "Paper Qualification": False,
            },
            "elimination_stage": stage,
            "paper_qualified": False,
            "status_final": "Rejeitada",
        }

    def _criteria_thresholds(self) -> dict[str, float]:
        return {
            "profit_factor": float(settings.validation.min_profit_factor),
            "sharpe": float(settings.validation.min_sharpe),
            "expectancy": float(settings.validation.min_expectancy),
            "net_profit": 0.0,
            "drawdown_pct": float(settings.validation.max_drawdown_pct) / 100.0,
            "win_rate": float(settings.validation.min_win_rate_pct) / 100.0,
            "number_of_trades": float(settings.validation.min_trades),
            "recovery_factor": 1.0,
            "robustness": 0.45,
            "stability": 0.45,
        }

    def _matrix_row(self, item: dict[str, Any]) -> dict[str, Any]:
        stages = item.get("stages", {})
        return {
            "strategy": item.get("strategy"),
            "Smoke Test": "OK" if stages.get("Smoke Test") else "FAIL",
            "Backtest": "OK" if stages.get("Backtest") else "FAIL",
            "Optimizer": "OK" if stages.get("Optimizer") else "FAIL",
            "Validation": "OK" if stages.get("Validation") else "FAIL",
            "Scientific Validation": "OK" if stages.get("Scientific Validation") else "FAIL",
            "Paper Qualification": "OK" if stages.get("Paper Qualification") else "FAIL",
            "Status Final": item.get("status_final"),
            "Elimination Stage": item.get("elimination_stage"),
        }

    def _distribution(
        self,
        rows: list[dict[str, Any]],
        key: str,
        limits: list[float],
        as_percent: bool = False,
    ) -> list[dict[str, Any]]:
        values = [float(x.get(key, 0.0)) for x in rows]
        bins = [-float("inf")] + limits + [float("inf")]
        out: list[dict[str, Any]] = []
        for i in range(len(bins) - 1):
            lo = bins[i]
            hi = bins[i + 1]
            count = len([v for v in values if v > lo and v <= hi])
            lo_txt = "-inf" if lo == -float("inf") else f"{lo * 100:.2f}%" if as_percent else f"{lo:.2f}"
            hi_txt = "+inf" if hi == float("inf") else f"{hi * 100:.2f}%" if as_percent else f"{hi:.2f}"
            out.append({"range": f"{lo_txt} - {hi_txt}", "count": count})
        return out

    def _context_benchmark(
        self,
        cfg: StrategyCatalogAuditConfig,
        strategy_names: list[str],
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        contexts: list[dict[str, Any]] = []
        by_strategy: dict[str, list[dict[str, Any]]] = {name: [] for name in strategy_names}

        for symbol in cfg.benchmark_symbols:
            for timeframe in cfg.benchmark_timeframes:
                df = self._load_df(symbol, timeframe, start_dt, end_dt, max_bars=cfg.max_bars)
                if df.empty:
                    contexts.append(
                        {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "status": "no_data",
                            "results": [],
                        }
                    )
                    continue

                result_rows: list[dict[str, Any]] = []
                for strategy_name in strategy_names:
                    try:
                        strategy = create_strategy(strategy_name)
                        strategy.initialize()
                        bt = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                            df,
                            symbol=symbol,
                            timeframe=timeframe,
                        )
                        m = bt.metrics.to_dict()
                        row = {
                            "strategy": strategy_name,
                            "profit_factor": float(m.get("profit_factor", 0.0)),
                            "sharpe": float(m.get("sharpe_ratio", 0.0)),
                            "expectancy": float(m.get("expectancy", 0.0)),
                            "drawdown_pct": abs(float(m.get("max_drawdown_pct", 0.0))),
                            "net_profit": float(m.get("net_profit", 0.0)),
                            "win_rate": float(m.get("win_rate", 0.0)),
                            "trades": int(m.get("total_trades", 0)),
                        }
                    except Exception as exc:
                        row = {
                            "strategy": strategy_name,
                            "error": str(exc),
                            "profit_factor": 0.0,
                            "sharpe": 0.0,
                            "expectancy": 0.0,
                            "drawdown_pct": 1.0,
                            "net_profit": 0.0,
                            "win_rate": 0.0,
                            "trades": 0,
                        }
                    result_rows.append(row)
                    by_strategy[strategy_name].append({"symbol": symbol, "timeframe": timeframe, **row})

                contexts.append({"symbol": symbol, "timeframe": timeframe, "status": "ok", "results": result_rows})

        edge_shift: list[dict[str, Any]] = []
        for strategy_name, samples in by_strategy.items():
            valid = [s for s in samples if int(s.get("trades", 0)) > 0]
            if not valid:
                edge_shift.append({"strategy": strategy_name, "best_context": None, "best_score": 0.0})
                continue
            best = sorted(valid, key=lambda x: self._context_score(x), reverse=True)[0]
            edge_shift.append(
                {
                    "strategy": strategy_name,
                    "best_context": f"{best.get('symbol')} {best.get('timeframe')}",
                    "best_score": round(self._context_score(best), 4),
                }
            )

        return {
            "contexts": contexts,
            "edge_shift": edge_shift,
        }

    def _context_score(self, row: dict[str, Any]) -> float:
        return (
            min(2.5, float(row.get("profit_factor", 0.0))) * 30.0
            + max(-2.0, min(3.0, float(row.get("sharpe", 0.0)))) * 10.0
            + max(-1.0, min(1.0, float(row.get("expectancy", 0.0)) / 50.0)) * 10.0
            + max(0.0, 1.0 - min(1.0, abs(float(row.get("drawdown_pct", 0.0))) / 0.35)) * 25.0
            + min(1.0, int(row.get("trades", 0)) / 100.0) * 25.0
        )

    def _group_by_distance(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        group_a = [x for x in rows if float(x.get("distance_to_approval_pct", 9999.0)) <= 10.0]
        group_b = [x for x in rows if 10.0 < float(x.get("distance_to_approval_pct", 9999.0)) <= 30.0]
        group_c = [x for x in rows if float(x.get("distance_to_approval_pct", 9999.0)) > 30.0]
        return {"group_a": group_a, "group_b": group_b, "group_c": group_c}

    def _top_rejection_reasons(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            for reason in row.get("rejection_reasons", []):
                counts[reason] = counts.get(reason, 0) + 1
        return [
            {"reason": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
        ]

    def _limits_audit(self, rows: list[dict[str, Any]], criteria: dict[str, float]) -> dict[str, Any]:
        if not rows:
            return {
                "compatible": False,
                "assessment": "Sem dados para avaliar limites.",
            }

        avg_pf = mean(float(x.get("profit_factor", 0.0)) for x in rows)
        avg_sharpe = mean(float(x.get("sharpe", 0.0)) for x in rows)
        avg_expectancy = mean(float(x.get("expectancy", 0.0)) for x in rows)
        avg_dd = mean(float(x.get("drawdown_pct", 0.0)) for x in rows)
        near = len([x for x in rows if float(x.get("distance_to_approval_pct", 9999.0)) <= 10.0])

        compatible = near > 0
        assessment = (
            "Limites compatíveis com BTCUSDT 5m em cripto; existem estrategias proximas da aprovacao."
            if compatible
            else "Limites atuais parecem excessivamente restritivos para BTCUSDT 5m em cripto, dado o perfil medio observado."
        )

        return {
            "compatible": compatible,
            "assessment": assessment,
            "avg_profit_factor": round(avg_pf, 4),
            "avg_sharpe": round(avg_sharpe, 4),
            "avg_expectancy": round(avg_expectancy, 4),
            "avg_drawdown_pct": round(avg_dd, 4),
            "thresholds": criteria,
        }

    def _primary_rejection_reason(
        self,
        distances: dict[str, dict[str, float | bool]],
        validation_pass: bool,
        optimizer_pass: bool,
    ) -> str:
        if not optimizer_pass:
            return "optimizer"
        if not validation_pass:
            return "validation"

        worst_name = "none"
        worst_gap = 0.0
        for key, data in distances.items():
            gap = max(0.0, -float(data.get("distance_pct", 0.0)))
            if gap > worst_gap:
                worst_gap = gap
                worst_name = key
        return worst_name

    def _distance_min(self, observed: float, required: float) -> float:
        denom = abs(required) if abs(required) > 1e-9 else 1.0
        return ((observed - required) / denom) * 100.0

    def _distance_max(self, observed: float, required: float) -> float:
        denom = abs(required) if abs(required) > 1e-9 else 1.0
        return ((required - observed) / denom) * 100.0

    def _recovery_factor(self, metrics: dict[str, Any]) -> float:
        net_profit = float(metrics.get("net_profit", 0.0))
        max_dd = abs(float(metrics.get("max_drawdown", 0.0)))
        if max_dd <= 1e-9:
            return 0.0 if net_profit <= 0 else 999.0
        return round(net_profit / max_dd, 4)

    def _robustness_score(self, cfg: StrategyCatalogAuditConfig, strategy_name: str, start_dt: datetime, end_dt: datetime) -> float:
        span = (end_dt - start_dt) / 3
        windows = [
            (start_dt, start_dt + span),
            (start_dt + span, start_dt + span * 2),
            (start_dt + span * 2, end_dt),
        ]

        pfs: list[float] = []
        for ws, we in windows:
            try:
                df = self._load_df(cfg.symbol, cfg.timeframe, ws, we, max_bars=cfg.max_bars)
                if len(df) < 120:
                    continue
                strategy = create_strategy(strategy_name)
                strategy.initialize()
                bt = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                    df,
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                )
                pfs.append(max(0.0, float(bt.metrics.profit_factor)))
            except Exception:
                continue

        if not pfs:
            return 0.0
        avg = mean(pfs)
        if avg <= 1e-9:
            return 0.0
        cv = pstdev(pfs) / avg if len(pfs) > 1 else 0.0
        score = max(0.0, min(1.0, (avg / (avg + 1.0)) * (1.0 - min(1.0, cv))))
        return round(score, 4)

    def _load_df(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        max_bars: int | None = None,
    ) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end)

        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(
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
        if max_bars and len(df) > int(max_bars):
            return df.tail(int(max_bars))
        return df

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}_ranking.csv"
        rows = report.get("ranking", [])
        if rows:
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "rank",
                        "strategy",
                        "profit_factor",
                        "sharpe",
                        "expectancy",
                        "net_profit",
                        "drawdown_pct",
                        "win_rate",
                        "number_of_trades",
                        "recovery_factor",
                        "robustness",
                        "stability",
                        "implementability",
                        "distance_to_approval_pct",
                        "group",
                        "elimination_stage",
                        "status_final",
                        "primary_rejection_reason",
                    ],
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(rows)
            outputs["csv"] = str(csv_path)

        matrix_path = self._results_dir / f"{prefix}_{stamp}_elimination_matrix.csv"
        matrix_rows = report.get("elimination_matrix", [])
        if matrix_rows:
            with matrix_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(matrix_rows[0].keys()))
                writer.writeheader()
                writer.writerows(matrix_rows)
            outputs["csv_elimination_matrix"] = str(matrix_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _to_markdown(self, report: dict[str, Any]) -> str:
        ranking = report.get("ranking", [])
        lines = [
            "# FASE 10.2 - AUDITORIA CIENTIFICA DO PIPELINE",
            "",
            f"- Run ID: {report.get('run_id')}",
            f"- Universo auditado: {len(ranking)} estrategias",
            f"- Contexto principal: {report.get('symbol')} {report.get('timeframe')} ({report.get('window_days')} dias)",
            f"- Recomendacao final: {report.get('recommendation')} - {report.get('recommendation_reason')}",
            "",
            "## Ranking completo",
            "",
            "| Rank | Estrategia | PF | Sharpe | Expectancy | DD | Win Rate | Recovery | Robustez | Stability | Implementability | Distancia Aprovacao | Grupo | Status |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        for row in ranking:
            lines.append(
                f"| {row.get('rank')} | {row.get('strategy')} | {float(row.get('profit_factor', 0.0)):.3f} | "
                f"{float(row.get('sharpe', 0.0)):.3f} | {float(row.get('expectancy', 0.0)):.3f} | "
                f"{float(row.get('drawdown_pct', 0.0)):.3f} | {float(row.get('win_rate', 0.0)):.3f} | "
                f"{float(row.get('recovery_factor', 0.0)):.3f} | {float(row.get('robustness', 0.0)):.3f} | "
                f"{float(row.get('stability', 0.0)):.3f} | {float(row.get('implementability', 0.0)):.3f} | "
                f"{float(row.get('distance_to_approval_pct', 0.0)):.2f}% | {row.get('group')} | {row.get('status_final')} |"
            )

        lines += [
            "",
            "## Matriz de eliminacao",
            "",
            "| Estrategia | Smoke Test | Backtest | Optimizer | Validation | Scientific Validation | Paper Qualification | Status Final | Etapa Eliminacao |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for row in report.get("elimination_matrix", []):
            lines.append(
                f"| {row.get('strategy')} | {row.get('Smoke Test')} | {row.get('Backtest')} | {row.get('Optimizer')} | "
                f"{row.get('Validation')} | {row.get('Scientific Validation')} | {row.get('Paper Qualification')} | "
                f"{row.get('Status Final')} | {row.get('Elimination Stage')} |"
            )

        lines += [
            "",
            "## Principais motivos de reprovacao",
            "",
        ]
        for r in report.get("rejection_reasons", []):
            lines.append(f"- {r.get('reason')}: {r.get('count')}")

        lines += [
            "",
            "## Distribuicoes",
            "",
            "```json",
            json.dumps(report.get("distributions", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Comparacao entre contextos",
            "",
            "```json",
            json.dumps(report.get("context_benchmark", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Distancia ate aprovacao",
            "",
            "```json",
            json.dumps(report.get("distance_to_approval", []), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Auditoria dos limites",
            "",
            "```json",
            json.dumps(report.get("limits_audit", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Decisao final",
            "",
            f"{report.get('recommendation')} - {report.get('recommendation_reason')}",
            "",
        ]

        return "\n".join(lines)

    def _persist_checkpoint(self, run_id: str, report: dict[str, Any]) -> None:
        try:
            with get_session() as session:
                history = HistoryPersistenceService(session)
                started_at = datetime.now(tz=timezone.utc)
                history.start_execution_session(
                    execution_id=run_id,
                    started_at=started_at,
                    status="completed",
                    host=socket.gethostname(),
                    cpu=platform.processor(),
                    workers=1,
                    python_version=platform.python_version(),
                    git_version=os.getenv("GIT_COMMIT"),
                )
                history.save_checkpoint(
                    execution_id=run_id,
                    stage="strategy_catalog_audit",
                    processed=len(report.get("ranking", [])),
                    completed=True,
                    payload={
                        "recommendation": report.get("recommendation"),
                        "group_a": len(report.get("groups", {}).get("group_a", [])),
                        "group_b": len(report.get("groups", {}).get("group_b", [])),
                        "group_c": len(report.get("groups", {}).get("group_c", [])),
                    },
                )
        except Exception as exc:
            logger.warning("Strategy catalog audit checkpoint persistence failed: %s", exc)
