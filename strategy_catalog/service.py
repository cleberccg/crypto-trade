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
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
from strategy_catalog.catalog import StrategyCatalog
from strategies.factory import create_strategy
from utils.logger import get_logger
from validation.validator import OptimizationValidator, ValidationCriteria, default_validation_window

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyCatalogCycleConfig:
    symbol: str
    timeframe: str
    window_days: int = 120
    initial_capital: float = 10_000.0
    max_catalog_strategies: int = 10
    smoke_bars: int = 500
    optimize_top_n: int = 5
    optimizer_max_combinations: int = 20
    optimizer_workers: int = 1
    top_k_for_paper: int = 3
    output_prefix: str = "strategy_catalog_cycle"


class StrategyCatalogCycleService:
    """Runs a permanent scientific cycle across catalog/discovered/experimental strategies."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: StrategyCatalogCycleConfig) -> dict[str, Any]:
        run_id = HistoryPersistenceService.new_execution_id()
        started_at = datetime.now(tz=timezone.utc)

        catalog = StrategyCatalog()
        catalog_map = {entry.name: entry for entry in catalog.entries()}
        classic_names = catalog.strategy_names()[: max(1, int(cfg.max_catalog_strategies))]
        discovered_names = ["TradeOutcomeNextGenV1", "TradeOutcomeNextGenV1.1"]
        experimental_names = ["TrendV1", "TrendV2", "MeanReversionV1", "BreakoutV1"]

        all_names = []
        for name in classic_names + discovered_names + experimental_names:
            if name not in all_names:
                all_names.append(name)

        start_dt = datetime.now(tz=timezone.utc) - timedelta(days=max(30, int(cfg.window_days)))
        end_dt = datetime.now(tz=timezone.utc)
        full_df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt)
        smoke_df = full_df.tail(max(200, int(cfg.smoke_bars))) if not full_df.empty else pd.DataFrame()

        ranking_rows: list[dict[str, Any]] = []
        ranking_by_name: dict[str, dict[str, Any]] = {}
        per_strategy_reports: list[dict[str, Any]] = []
        survivors: list[dict[str, Any]] = []
        eliminated_count = 0

        if full_df.empty:
            report = {
                "phase": "10",
                "run_id": run_id,
                "generated_at": started_at.isoformat(),
                "symbol": cfg.symbol,
                "timeframe": cfg.timeframe,
                "window_days": cfg.window_days,
                "strategies_added_to_catalog": len(classic_names),
                "pipeline": ["Implementada", "Smoke Test", "Backtest", "Otimizada", "Validada", "Paper Trading", "Produção", "Rejeitada"],
                "ranking": [],
                "top10": [],
                "top3_for_paper": [],
                "comparison": {},
                "per_strategy_reports": [],
                "catalog_entries": [entry.__dict__ for entry in catalog.entries()],
                "recommendation": "Sem dados de candles para executar o ciclo científico.",
            }
            outputs = self._write_outputs(cfg.output_prefix, report)
            self._persist_checkpoint(run_id, report)
            return {
                "summary": {
                    "status": "completed",
                    "run_id": run_id,
                    "catalog_size": len(classic_names),
                    "total_strategies_evaluated": 0,
                    "qualified_for_paper": 0,
                    "top3_prepared": [],
                    "top_strategy": None,
                },
                "report": report,
                "outputs": outputs,
            }

        for index, strategy_name in enumerate(all_names, start=1):
            logger.info("Catalog cycle evaluating %d/%d strategy=%s", index, len(all_names), strategy_name)
            category = self._category_of(strategy_name)
            origin, family = self._metadata_for_strategy(strategy_name, category, catalog_map)

            smoke = self._smoke_test(cfg, strategy_name, smoke_df)
            if not smoke.get("passed"):
                eliminated_count += 1
                row = {
                    "strategy": strategy_name,
                    "category": category,
                    "origin": origin,
                    "family": family,
                    "status": "Rejeitada",
                    "profit_factor": 0.0,
                    "sharpe": 0.0,
                    "expectancy": 0.0,
                    "drawdown_pct": 1.0,
                    "net_profit": 0.0,
                    "win_rate": 0.0,
                    "number_of_trades": 0,
                    "robustness": 0.0,
                    "stability": 0.0,
                    "operational_capacity": 0,
                    "trade_outcome_score": 0.0,
                    "validation_passed": 0,
                    "paper_qualified": False,
                    "ranking_score": -500.0,
                    "eliminated_reason": str(smoke.get("reason", "smoke_test_failed")),
                }
                ranking_rows.append(row)
                ranking_by_name[strategy_name] = row
                per_strategy_reports.append(
                    {
                        "strategy": strategy_name,
                        "category": category,
                        "origin": origin,
                        "family": family,
                        "status": "Rejeitada",
                        "smoke": smoke,
                    }
                )
                continue

            try:
                strategy = create_strategy(strategy_name)
                strategy.initialize()
                bt = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                    full_df,
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                )
                metrics = bt.metrics.to_dict()
            except Exception as exc:
                eliminated_count += 1
                row = {
                    "strategy": strategy_name,
                    "category": category,
                    "origin": origin,
                    "family": family,
                    "status": "Rejeitada",
                    "profit_factor": 0.0,
                    "sharpe": 0.0,
                    "expectancy": 0.0,
                    "drawdown_pct": 1.0,
                    "net_profit": 0.0,
                    "win_rate": 0.0,
                    "number_of_trades": 0,
                    "robustness": 0.0,
                    "stability": 0.0,
                    "operational_capacity": 0,
                    "trade_outcome_score": 0.0,
                    "validation_passed": 0,
                    "paper_qualified": False,
                    "ranking_score": -450.0,
                    "eliminated_reason": f"backtest_error:{exc}",
                }
                ranking_rows.append(row)
                ranking_by_name[strategy_name] = row
                per_strategy_reports.append(
                    {
                        "strategy": strategy_name,
                        "category": category,
                        "origin": origin,
                        "family": family,
                        "status": "Rejeitada",
                        "smoke": smoke,
                        "error": str(exc),
                    }
                )
                continue

            pre_score = self._preselection_score(metrics)
            row = {
                "strategy": strategy_name,
                "category": category,
                "origin": origin,
                "family": family,
                "status": "Backtest",
                "profit_factor": float(metrics.get("profit_factor", 0.0)),
                "sharpe": float(metrics.get("sharpe_ratio", 0.0)),
                "expectancy": float(metrics.get("expectancy", 0.0)),
                "drawdown_pct": abs(float(metrics.get("max_drawdown_pct", 0.0))),
                "net_profit": float(metrics.get("net_profit", 0.0)),
                "win_rate": float(metrics.get("win_rate", 0.0)),
                "number_of_trades": int(metrics.get("total_trades", 0)),
                "robustness": 0.0,
                "stability": 0.0,
                "operational_capacity": int(metrics.get("total_trades", 0)),
                "trade_outcome_score": 0.0,
                "validation_passed": 0,
                "paper_qualified": False,
                "ranking_score": pre_score,
                "eliminated_reason": "",
            }

            ranking_rows.append(row)
            ranking_by_name[strategy_name] = row

            if self._is_clearly_bad(metrics):
                eliminated_count += 1
                row["status"] = "Rejeitada"
                row["ranking_score"] = pre_score - 200.0
                row["eliminated_reason"] = "clear_underperformance_after_backtest"
                per_strategy_reports.append(
                    {
                        "strategy": strategy_name,
                        "category": category,
                        "origin": origin,
                        "family": family,
                        "status": "Rejeitada",
                        "smoke": smoke,
                        "backtest_metrics": metrics,
                    }
                )
                continue

            survivors.append(
                {
                    "strategy": strategy_name,
                    "category": category,
                    "origin": origin,
                    "family": family,
                    "backtest_metrics": metrics,
                    "pre_score": pre_score,
                }
            )
            per_strategy_reports.append(
                {
                    "strategy": strategy_name,
                    "category": category,
                    "origin": origin,
                    "family": family,
                    "status": "Backtest",
                    "smoke": smoke,
                    "backtest_metrics": metrics,
                }
            )

        shortlist_n = min(len(survivors), max(3, min(5, int(cfg.optimize_top_n)))) if survivors else 0
        shortlist = sorted(survivors, key=lambda x: float(x.get("pre_score", 0.0)), reverse=True)[:shortlist_n]
        shortlist_names = {str(x.get("strategy")) for x in shortlist}

        for row in ranking_rows:
            if row.get("strategy") in shortlist_names and row.get("status") != "Rejeitada":
                row["status"] = "Otimizada"

        for candidate in shortlist:
            strategy_name = str(candidate.get("strategy"))
            base_metrics = dict(candidate.get("backtest_metrics", {}))

            optimizer = StrategyOptimizer(output_dir=self._results_dir)
            opt_cfg = OptimizerRunConfig(
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                start=start_dt,
                end=end_dt,
                capital=cfg.initial_capital,
                top_n=5,
                workers=max(1, int(cfg.optimizer_workers)),
                max_combinations=max(5, int(cfg.optimizer_max_combinations)),
                diagnostic=False,
                strategy_name=strategy_name,
                strategy_version="v1",
            )
            try:
                opt_summary = optimizer.run(opt_cfg)
                opt_results = opt_summary.top_results[:5]
            except Exception as exc:
                logger.warning("Optimizer failed for %s: %s", strategy_name, exc)
                opt_summary = None
                opt_results = []

            criteria = ValidationCriteria(
                min_trades=max(3, int(settings.validation.min_trades // 10)),
                min_profit_factor=0.9,
                max_drawdown_pct=max(20.0, float(settings.validation.max_drawdown_pct)),
                min_win_rate_pct=20.0,
                min_expectancy=-0.5,
                min_sharpe=-2.0,
            )
            validator = OptimizationValidator(criteria=criteria, output_dir=self._results_dir, strategy_name=strategy_name)
            val_window = default_validation_window(start_dt, end_dt, symbol=cfg.symbol, timeframe=cfg.timeframe)
            try:
                val_summary = validator.validate(
                    optimization_results=opt_results,
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                    capital=cfg.initial_capital,
                    train_start=val_window.train_start,
                    train_end=val_window.train_end,
                    validation_start=val_window.validation_start,
                    validation_end=val_window.validation_end,
                    top_n=5,
                )
            except Exception as exc:
                logger.warning("Validation failed for %s: %s", strategy_name, exc)
                val_summary = None

            robustness_score = self._robustness_score(cfg, strategy_name, start_dt, end_dt)
            trade_outcome_score = self._trade_outcome_score(base_metrics)
            paper_qualified = (
                float(base_metrics.get("profit_factor", 0.0)) >= 1.0
                and float(base_metrics.get("expectancy", 0.0)) >= 0.0
                and abs(float(base_metrics.get("max_drawdown_pct", 0.0))) <= 0.30
                and robustness_score >= 0.45
                and trade_outcome_score >= 50.0
                and bool(val_summary and int(val_summary.passed) > 0)
            )

            final_status = "Paper Trading" if paper_qualified else "Validada"
            if not (val_summary and int(val_summary.passed) > 0):
                final_status = "Otimizada"

            ranking_score = self._ranking_score(base_metrics, robustness_score, trade_outcome_score, paper_qualified)
            row = ranking_by_name.get(strategy_name)
            if row:
                row["status"] = final_status
                row["robustness"] = robustness_score
                row["stability"] = robustness_score
                row["trade_outcome_score"] = trade_outcome_score
                row["validation_passed"] = int(val_summary.passed) if val_summary else 0
                row["paper_qualified"] = paper_qualified
                row["ranking_score"] = ranking_score

            for strategy_report in per_strategy_reports:
                if strategy_report.get("strategy") == strategy_name:
                    strategy_report["status"] = final_status
                    strategy_report["optimizer"] = {
                        "tested": int(opt_summary.combinations_tested) if opt_summary else 0,
                        "discarded": int(opt_summary.combinations_discarded) if opt_summary else 0,
                    }
                    strategy_report["validation"] = {
                        "passed": int(val_summary.passed) if val_summary else 0,
                        "discarded": int(val_summary.discarded) if val_summary else 0,
                    }
                    strategy_report["robustness_score"] = robustness_score
                    strategy_report["trade_outcome_score"] = trade_outcome_score
                    strategy_report["paper_qualified"] = paper_qualified
                    break

        ranking_rows = sorted(ranking_rows, key=lambda r: float(r.get("ranking_score", 0.0)), reverse=True)
        for i, row in enumerate(ranking_rows, start=1):
            row["rank"] = i

        qualified = [r for r in ranking_rows if bool(r.get("paper_qualified", False))]
        top10 = ranking_rows[:10]
        top3_for_paper = qualified[: max(1, int(cfg.top_k_for_paper))]

        comparison = {
            "classic_vs_discovered": self._compare_group(ranking_rows, "classic", "discovered_auto"),
            "classic_vs_experimental": self._compare_group(ranking_rows, "classic", "experimental"),
        }

        report = {
            "phase": "10",
            "run_id": run_id,
            "generated_at": started_at.isoformat(),
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "window_days": cfg.window_days,
            "strategies_added_to_catalog": len(classic_names),
            "pipeline": [
                "Implementada",
                "Smoke Test",
                "Backtest",
                "Otimizada",
                "Validada",
                "Paper Trading",
                "Produção",
                "Rejeitada",
            ],
            "funnel": {
                "implemented": len(all_names),
                "smoke_passed": len([r for r in ranking_rows if r.get("status") != "Rejeitada"]),
                "backtest_passed": len(survivors),
                "optimized": len(shortlist_names),
                "validated": len([r for r in ranking_rows if r.get("status") in {"Validada", "Paper Trading"}]),
                "paper_trading": len(qualified),
                "rejected": eliminated_count,
            },
            "ranking": ranking_rows,
            "top10": top10,
            "top3_for_paper": top3_for_paper,
            "comparison": comparison,
            "per_strategy_reports": per_strategy_reports,
            "catalog_entries": [entry.__dict__ for entry in catalog.entries()],
            "second_batch_backlog": ["SuperTrend"],
            "recommendation": self._build_recommendation(top3_for_paper),
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        self._persist_checkpoint(run_id, report)

        summary = {
            "status": "completed",
            "run_id": run_id,
            "catalog_size": len(classic_names),
            "total_strategies_evaluated": len(ranking_rows),
            "eliminated_after_backtest": eliminated_count,
            "optimized_shortlist": len(shortlist_names),
            "qualified_for_paper": len(qualified),
            "top3_prepared": [x.get("strategy") for x in top3_for_paper],
            "top_strategy": top10[0]["strategy"] if top10 else None,
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _evaluate_strategy(
        self,
        cfg: StrategyCatalogCycleConfig,
        strategy_name: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        category = self._category_of(strategy_name)

        strategy = create_strategy(strategy_name)
        strategy.initialize()

        df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt)
        if df.empty:
            return {
                "strategy": strategy_name,
                "ranking_row": {
                    "strategy": strategy_name,
                    "category": category,
                    "ranking_score": -999.0,
                    "paper_qualified": False,
                    "error": "no_data",
                },
            }

        # 1) Backtest
        engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital))
        bt = engine.run(df, symbol=cfg.symbol, timeframe=cfg.timeframe)

        # 2) Optimizer (small controlled budget)
        optimizer = StrategyOptimizer(output_dir=self._results_dir)
        opt_cfg = OptimizerRunConfig(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            start=start_dt,
            end=end_dt,
            capital=cfg.initial_capital,
            top_n=5,
            workers=max(1, int(cfg.optimizer_workers)),
            max_combinations=max(5, int(cfg.optimizer_max_combinations)),
            diagnostic=False,
            strategy_name=strategy_name,
            strategy_version="v1",
        )
        try:
            opt_summary = optimizer.run(opt_cfg)
            opt_results = opt_summary.top_results[:5]
        except Exception as exc:
            logger.warning("Optimizer failed for %s: %s", strategy_name, exc)
            opt_summary = None
            opt_results = []

        # 3) Validation
        criteria = ValidationCriteria(
            min_trades=max(3, int(settings.validation.min_trades // 10)),
            min_profit_factor=0.9,
            max_drawdown_pct=max(20.0, float(settings.validation.max_drawdown_pct)),
            min_win_rate_pct=20.0,
            min_expectancy=-0.5,
            min_sharpe=-2.0,
        )
        validator = OptimizationValidator(criteria=criteria, output_dir=self._results_dir, strategy_name=strategy_name)
        val_window = default_validation_window(start_dt, end_dt, symbol=cfg.symbol, timeframe=cfg.timeframe)
        try:
            val_summary = validator.validate(
                optimization_results=opt_results,
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                capital=cfg.initial_capital,
                train_start=val_window.train_start,
                train_end=val_window.train_end,
                validation_start=val_window.validation_start,
                validation_end=val_window.validation_end,
                top_n=5,
            )
        except Exception as exc:
            logger.warning("Validation failed for %s: %s", strategy_name, exc)
            val_summary = None

        # 4) Scientific robustness (internal standardized score)
        robustness_score = self._robustness_score(cfg, strategy_name, start_dt, end_dt)

        # 5) Trade outcome evaluation (internal standardized score)
        trade_outcome_score = self._trade_outcome_score(bt.metrics.to_dict())

        # 6) Paper trading qualification
        paper_qualified = (
            float(bt.metrics.profit_factor) >= 1.0
            and float(bt.metrics.expectancy) >= 0.0
            and abs(float(bt.metrics.max_drawdown_pct)) <= 0.30
            and robustness_score >= 0.45
            and trade_outcome_score >= 50.0
        )

        # 7) Ranking score (single unified ranking)
        ranking_score = self._ranking_score(bt.metrics.to_dict(), robustness_score, trade_outcome_score, paper_qualified)

        row = {
            "strategy": strategy_name,
            "category": category,
            "profit_factor": float(bt.metrics.profit_factor),
            "sharpe": float(bt.metrics.sharpe_ratio),
            "expectancy": float(bt.metrics.expectancy),
            "drawdown_pct": abs(float(bt.metrics.max_drawdown_pct)),
            "net_profit": float(bt.metrics.net_profit),
            "win_rate": float(bt.metrics.win_rate),
            "number_of_trades": int(bt.metrics.total_trades),
            "robustness": robustness_score,
            "stability": robustness_score,
            "operational_capacity": int(bt.metrics.total_trades),
            "trade_outcome_score": trade_outcome_score,
            "validation_passed": int(val_summary.passed) if val_summary else 0,
            "paper_qualified": paper_qualified,
            "ranking_score": ranking_score,
        }

        return {
            "strategy": strategy_name,
            "category": category,
            "backtest_metrics": bt.metrics.to_dict(),
            "optimizer": {
                "tested": int(opt_summary.combinations_tested) if opt_summary else 0,
                "discarded": int(opt_summary.combinations_discarded) if opt_summary else 0,
            },
            "validation": {
                "passed": int(val_summary.passed) if val_summary else 0,
                "discarded": int(val_summary.discarded) if val_summary else 0,
            },
            "robustness_score": robustness_score,
            "trade_outcome_score": trade_outcome_score,
            "paper_qualified": paper_qualified,
            "ranking_row": row,
        }

    def _metadata_for_strategy(
        self,
        strategy_name: str,
        category: str,
        catalog_map: dict[str, Any],
    ) -> tuple[str, str]:
        entry = catalog_map.get(strategy_name)
        if entry is not None:
            return str(entry.origin), str(entry.family)
        if category == "discovered_auto":
            return "Descoberta da Plataforma", "Momentum"
        return "Open Source", "Tendência"

    def _smoke_test(self, cfg: StrategyCatalogCycleConfig, strategy_name: str, df: pd.DataFrame) -> dict[str, Any]:
        if df.empty or len(df) < 50:
            return {"passed": False, "reason": "insufficient_data", "signals": 0, "trades": 0}
        try:
            strategy = create_strategy(strategy_name)
            strategy.initialize()
            bt = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                df,
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
            )
            trades = int(bt.metrics.total_trades)
            return {
                "passed": trades > 0,
                "reason": "ok" if trades > 0 else "no_trades",
                "signals": trades,
                "trades": trades,
            }
        except Exception as exc:
            return {"passed": False, "reason": f"exception:{exc}", "signals": 0, "trades": 0}

    def _is_clearly_bad(self, metrics: dict[str, Any]) -> bool:
        trades = int(metrics.get("total_trades", 0))
        pf = float(metrics.get("profit_factor", 0.0))
        expectancy = float(metrics.get("expectancy", 0.0))
        drawdown = abs(float(metrics.get("max_drawdown_pct", 0.0)))
        net_profit = float(metrics.get("net_profit", 0.0))
        if trades < 3:
            return True
        if pf < 0.80 and expectancy < 0.0:
            return True
        if drawdown > 0.45 and net_profit <= 0.0:
            return True
        return False

    def _preselection_score(self, metrics: dict[str, Any]) -> float:
        pf = max(0.0, float(metrics.get("profit_factor", 0.0)))
        sharpe = float(metrics.get("sharpe_ratio", 0.0))
        expectancy = float(metrics.get("expectancy", 0.0))
        drawdown = abs(float(metrics.get("max_drawdown_pct", 0.0)))
        trades = max(0, int(metrics.get("total_trades", 0)))
        score = (
            min(2.0, pf) * 25.0
            + max(-1.5, min(3.0, sharpe)) * 7.5
            + max(-1.0, min(1.0, expectancy / 50.0)) * 15.0
            + max(0.0, 1.0 - min(1.0, drawdown / 0.35)) * 25.0
            + min(1.0, trades / 150.0) * 15.0
        )
        return round(score, 4)

    def _robustness_score(self, cfg: StrategyCatalogCycleConfig, strategy_name: str, start_dt: datetime, end_dt: datetime) -> float:
        # Split into 3 equal windows and measure PF stability
        span = (end_dt - start_dt) / 3
        windows = [
            (start_dt, start_dt + span),
            (start_dt + span, start_dt + span * 2),
            (start_dt + span * 2, end_dt),
        ]
        pfs: list[float] = []
        for ws, we in windows:
            try:
                df = self._load_df(cfg.symbol, cfg.timeframe, ws, we)
                if len(df) < 120:
                    continue
                st = create_strategy(strategy_name)
                st.initialize()
                bt = BacktestEngine(st, config=BacktestConfig(initial_capital=cfg.initial_capital)).run(
                    df, symbol=cfg.symbol, timeframe=cfg.timeframe
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
        # Higher PF and lower CV => higher robustness in [0,1]
        score = max(0.0, min(1.0, (avg / (avg + 1.0)) * (1.0 - min(1.0, cv))))
        return round(score, 4)

    def _trade_outcome_score(self, metrics: dict[str, Any]) -> float:
        pf = max(0.0, float(metrics.get("profit_factor", 0.0)))
        wr = max(0.0, min(1.0, float(metrics.get("win_rate", 0.0))))
        exp = float(metrics.get("expectancy", 0.0))
        dd = abs(float(metrics.get("max_drawdown_pct", 0.0)))
        # Scaled score in [0,100]
        score = (
            min(2.0, pf) / 2.0 * 40.0
            + wr * 25.0
            + max(-1.0, min(1.0, exp / 50.0)) * 15.0
            + max(0.0, 1.0 - min(1.0, dd / 0.30)) * 20.0
        )
        return round(max(0.0, min(100.0, score)), 2)

    def _ranking_score(
        self,
        metrics: dict[str, Any],
        robustness: float,
        outcome_score: float,
        qualified: bool,
    ) -> float:
        pf = max(0.0, float(metrics.get("profit_factor", 0.0)))
        sharpe = float(metrics.get("sharpe_ratio", 0.0))
        expectancy = float(metrics.get("expectancy", 0.0))
        dd = abs(float(metrics.get("max_drawdown_pct", 0.0)))
        trades = int(metrics.get("total_trades", 0))

        score = (
            min(2.5, pf) * 20.0
            + max(-2.0, min(4.0, sharpe)) * 5.0
            + max(-1.0, min(1.0, expectancy / 50.0)) * 10.0
            + max(0.0, 1.0 - min(1.0, dd / 0.35)) * 20.0
            + min(1.0, trades / 200.0) * 15.0
            + robustness * 20.0
            + (outcome_score / 100.0) * 10.0
            + (10.0 if qualified else 0.0)
        )
        return round(score, 4)

    def _category_of(self, strategy_name: str) -> str:
        if strategy_name.startswith("Classic"):
            return "classic"
        if strategy_name.startswith("TradeOutcome"):
            return "discovered_auto"
        return "experimental"

    def _compare_group(self, rows: list[dict[str, Any]], left: str, right: str) -> dict[str, Any]:
        l = [r for r in rows if r.get("category") == left]
        r = [r for r in rows if r.get("category") == right]
        if not l or not r:
            return {"left": left, "right": right, "status": "insufficient_data"}

        def _avg(group: list[dict[str, Any]], key: str) -> float:
            vals = [float(x.get(key, 0.0)) for x in group]
            return sum(vals) / len(vals)

        return {
            "left": left,
            "right": right,
            "avg_profit_factor_left": _avg(l, "profit_factor"),
            "avg_profit_factor_right": _avg(r, "profit_factor"),
            "avg_sharpe_left": _avg(l, "sharpe"),
            "avg_sharpe_right": _avg(r, "sharpe"),
            "avg_expectancy_left": _avg(l, "expectancy"),
            "avg_expectancy_right": _avg(r, "expectancy"),
            "avg_drawdown_left": _avg(l, "drawdown_pct"),
            "avg_drawdown_right": _avg(r, "drawdown_pct"),
            "avg_robustness_left": _avg(l, "robustness"),
            "avg_robustness_right": _avg(r, "robustness"),
        }

    def _build_recommendation(self, top3_for_paper: list[dict[str, Any]]) -> str:
        if not top3_for_paper:
            return "Nenhuma estratégia atingiu critérios mínimos para Paper Trading neste ciclo."
        names = ", ".join(str(x.get("strategy")) for x in top3_for_paper)
        return f"Preparar exclusivamente para Paper Trading: {names}. Não adicionar novas estratégias até validação operacional." 

    def _load_df(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end)
        if not candles:
            return pd.DataFrame()
        return pd.DataFrame(
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
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            outputs["csv"] = str(csv_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)

        catalog_md = self._base_dir / "docs" / "STRATEGY_CATALOG.md"
        catalog_md.write_text(self._catalog_markdown(report), encoding="utf-8")
        outputs["strategy_catalog_md"] = str(catalog_md)
        return outputs

    def _to_markdown(self, report: dict[str, Any]) -> str:
        top10 = report.get("top10", [])
        top3 = report.get("top3_for_paper", [])
        funnel = report.get("funnel", {})
        lines = [
            "# FASE 10 — Catálogo Científico Permanente de Estratégias",
            "",
            f"- Estratégias adicionadas ao catálogo: **{report.get('strategies_added_to_catalog', 0)}**",
            f"- Estratégias avaliadas no ciclo: **{len(report.get('ranking', []))}**",
            f"- Funil: Implementadas={funnel.get('implemented', 0)} | Smoke={funnel.get('smoke_passed', 0)} | Backtest={funnel.get('backtest_passed', 0)} | Otimizadas={funnel.get('optimized', 0)} | Validadas={funnel.get('validated', 0)} | Paper={funnel.get('paper_trading', 0)} | Rejeitadas={funnel.get('rejected', 0)}",
            "",
            "## Top 10",
            "",
            "| Rank | Estratégia | Categoria | Origem | Família | Status | PF | Sharpe | Expectancy | Drawdown | Robustez | Qualificada Paper |",
            "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
        for row in top10:
            lines.append(
                f"| {row.get('rank')} | {row.get('strategy')} | {row.get('category')} | "
                f"{row.get('origin')} | {row.get('family')} | {row.get('status')} | "
                f"{float(row.get('profit_factor', 0.0)):.3f} | {float(row.get('sharpe', 0.0)):.3f} | "
                f"{float(row.get('expectancy', 0.0)):.3f} | {float(row.get('drawdown_pct', 0.0)):.3f} | "
                f"{float(row.get('robustness', 0.0)):.3f} | {'Sim' if row.get('paper_qualified') else 'Não'} |"
            )

        lines += [
            "",
            "## Top 3 Recomendadas para Paper Trading",
            "",
        ]
        if top3:
            for idx, row in enumerate(top3, start=1):
                lines.append(f"{idx}. {row.get('strategy')} ({row.get('category')})")
        else:
            lines.append("Nenhuma estratégia aprovada para Paper Trading neste ciclo.")

        lines += [
            "",
            "## Comparação de grupos",
            "",
            "```json",
            json.dumps(report.get("comparison", {}), ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            f"Recomendação final: {report.get('recommendation', '')}",
        ]
        return "\n".join(lines) + "\n"

    def _catalog_markdown(self, report: dict[str, Any]) -> str:
        entries = report.get("catalog_entries", [])
        ranking = {row.get("strategy"): row for row in report.get("ranking", [])}
        lines = [
            "# STRATEGY_CATALOG",
            "",
            "Catálogo científico permanente de estratégias da plataforma.",
            "",
            "| Estratégia | Origem | Referência | Família | Indicadores | Parâmetros padrão | Status |",
            "|---|---|---|---|---|---|---|",
        ]
        for e in entries:
            name = e.get("name")
            row = ranking.get(name, {})
            status = row.get("status") or e.get("lifecycle_status") or "Implementada"
            lines.append(
                f"| {name} | {e.get('origin')} | {e.get('reference')} | {e.get('family')} | "
                f"{', '.join(e.get('indicators', []))} | {json.dumps(e.get('default_parameters', {}), ensure_ascii=False)} | {status} |"
            )
        return "\n".join(lines) + "\n"

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
                    stage="strategy_catalog_cycle",
                    processed=len(report.get("ranking", [])),
                    completed=True,
                    payload={
                        "top3": [r.get("strategy") for r in report.get("top3_for_paper", [])],
                        "qualified": len([r for r in report.get("ranking", []) if r.get("paper_qualified")]),
                    },
                )
        except Exception as exc:
            logger.warning("Strategy catalog checkpoint persistence failed: %s", exc)
