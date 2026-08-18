from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy.orm import Session

from config.settings import settings
from core.events import EventBus
from core.events.listeners import HistoryListener, LogListener, MetricsListener
from database.connection import get_session
from database.history_models import TradeOutcomeImplementationRun
from database.history_repositories import TradeOutcomeImplementationRunRepository
from database.history_service import HistoryPersistenceService
from optimizer.optimization_result import OptimizationResult
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
from research.services.strategy_research_lab import ResearchLabConfig, StrategyResearchLab
from research.services.trade_management_research_lab import TradeManagementLabConfig, TradeManagementResearchLab
from strategies.trade_outcome_nextgen_v1 import TradeOutcomeNextGenV1Strategy
from utils.logger import get_logger
from utils.metrics import expectancy_from_pnl, max_drawdown_from_pnl, profit_factor_from_pnl, sharpe_from_pnl, win_rate_from_pnl
from validation.validator import OptimizationValidator, ValidationCriteria

logger = get_logger(__name__)


@dataclass(frozen=True)
class TradeOutcomeControlledImplementationConfig:
    events_glob: str = "optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"
    trade_outcome_csv: str = ""
    strategy_name: str = "TradeOutcomeNextGenV1"
    target_name: str = "return_above"
    approved_rule: str = "distance_to_ema_pct<=0.162026"
    distance_threshold: float = 0.162026
    fidelity_min_f1: float = 0.95
    optimizer_max_combinations: int = 60
    optimizer_workers: int = 4
    optimizer_capital: float = 10_000.0
    output_prefix: str = "trade_outcome_controlled_implementation"
    run_optimizer_validation: bool = True
    run_research_labs: bool = True
    persist_to_db: bool = True


class TradeOutcomeControlledImplementationService:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, config: TradeOutcomeControlledImplementationConfig | None = None) -> dict[str, Any]:
        cfg = config or TradeOutcomeControlledImplementationConfig()
        started = datetime.now(timezone.utc)

        strategy = TradeOutcomeNextGenV1Strategy(distance_threshold=cfg.distance_threshold)
        strategy.initialize()

        candidate = self._load_approved_candidate(cfg)
        events = self._load_events(cfg.events_glob)
        fidelity = self._run_fidelity_audit(events, strategy, cfg)

        if fidelity["f1"] < cfg.fidelity_min_f1:
            decision = "OPCAO_B"
            report = self._build_report(
                cfg=cfg,
                started=started,
                decision=decision,
                candidate=candidate,
                fidelity=fidelity,
                backtest_observed={},
                degradation={},
                optimizer_summary={},
                validation_summary={},
                strategy_lab_summary={},
                trade_lab_summary={},
                notes=["Fidelity gate failed. Pipeline interrupted before backtest/validation/labs."],
            )
            outputs = self._write_outputs(report, cfg.output_prefix)
            self._persist(run_id=str(uuid4()), cfg=cfg, decision=decision, candidate=candidate, fidelity=fidelity, report=report, outputs=outputs)
            return {
                "summary": {
                    "decision": decision,
                    "fidelity_f1": fidelity["f1"],
                    "fidelity_gate": False,
                },
                "outputs": outputs,
                "report": report,
            }

        backtest_observed, degradation = self._run_event_backtest(events, strategy, candidate)

        optimizer_summary: dict[str, Any] = {}
        validation_summary: dict[str, Any] = {}
        strategy_lab_summary: dict[str, Any] = {}
        trade_lab_summary: dict[str, Any] = {}

        if cfg.run_optimizer_validation:
            optimizer_summary, validation_summary = self._run_optimizer_and_validation(cfg)

        if cfg.run_research_labs:
            strategy_lab_summary, trade_lab_summary = self._run_research_labs(cfg)

        decision = self._decide(fidelity=fidelity, degradation=degradation, validation_summary=validation_summary)
        report = self._build_report(
            cfg=cfg,
            started=started,
            decision=decision,
            candidate=candidate,
            fidelity=fidelity,
            backtest_observed=backtest_observed,
            degradation=degradation,
            optimizer_summary=optimizer_summary,
            validation_summary=validation_summary,
            strategy_lab_summary=strategy_lab_summary,
            trade_lab_summary=trade_lab_summary,
            notes=[],
        )
        outputs = self._write_outputs(report, cfg.output_prefix)
        run_id = str(uuid4())
        self._persist(run_id=run_id, cfg=cfg, decision=decision, candidate=candidate, fidelity=fidelity, report=report, outputs=outputs)

        return {
            "summary": {
                "run_id": run_id,
                "decision": decision,
                "fidelity_f1": fidelity["f1"],
                "observed_profit_factor": backtest_observed.get("profit_factor"),
                "validation_approved": validation_summary.get("approved", 0),
            },
            "outputs": outputs,
            "report": report,
        }

    def _load_approved_candidate(self, cfg: TradeOutcomeControlledImplementationConfig) -> dict[str, Any]:
        csv_path = Path(cfg.trade_outcome_csv) if cfg.trade_outcome_csv else self._latest_trade_outcome_csv()
        if not csv_path.exists():
            raise ValueError(f"Trade outcome CSV not found: {csv_path}")

        df = pd.read_csv(csv_path)
        if df.empty:
            raise ValueError("Trade outcome CSV is empty.")

        approved = df[df["approved"].astype(bool)]
        if approved.empty:
            raise ValueError("No approved rows found in trade outcome CSV.")

        approved = approved[approved["target"].astype(str) == cfg.target_name]
        approved = approved[approved["rule"].astype(str) == cfg.approved_rule]
        if approved.empty:
            raise ValueError(
                "Approved candidate not found using target/rule filter: "
                f"target={cfg.target_name} rule={cfg.approved_rule}"
            )

        row = approved.sort_values(["trade_outcome_score", "support"], ascending=[False, False]).iloc[0]
        return {
            "target": str(row["target"]),
            "rule": str(row["rule"]),
            "support": int(row["support"]),
            "trade_outcome_score": float(row["trade_outcome_score"]),
            "scientific_robustness_score": float(row["scientific_robustness_score"]),
            "expected_profit_factor": float(row.get("expected_profit_factor", 0.0)),
            "expected_sharpe": float(row.get("expected_sharpe", 0.0)),
            "expected_expectancy": float(row.get("expected_expectancy", 0.0)),
            "expected_drawdown": float(row.get("expected_drawdown", 0.0)),
            "confidence": float(row.get("confidence", 0.0)),
            "generalization_score": float(row.get("generalization_score", 0.0)),
            "csv_path": str(csv_path),
        }

    def _latest_trade_outcome_csv(self) -> Path:
        candidates = sorted(
            (self._base_dir / "optimization" / "results").glob("trade_outcome_learning_*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise ValueError("No trade_outcome_learning_*.csv artifact found.")
        return candidates[0]

    def _load_events(self, events_glob: str) -> pd.DataFrame:
        files = sorted(self._base_dir.glob(events_glob))
        if not files:
            raise ValueError(f"No event CSV files found for glob: {events_glob}")

        usecols = [
            "open_time",
            "symbol",
            "timeframe",
            "distance_to_ema_pct",
            "future_return",
            "future_return_20",
            "drawdown",
        ]
        parts: list[pd.DataFrame] = []
        for path in files:
            chunk = pd.read_csv(path, low_memory=False)
            cols = [c for c in usecols if c in chunk.columns]
            if not cols:
                continue
            chunk = chunk[cols].copy()
            if "future_return_20" not in chunk.columns:
                chunk["future_return_20"] = pd.to_numeric(chunk.get("future_return"), errors="coerce").fillna(0.0) * 1.2
            if "drawdown" not in chunk.columns:
                chunk["drawdown"] = 0.0
            parts.append(chunk)

        if not parts:
            raise ValueError("Event dataset is empty after loading files.")

        frame = pd.concat(parts, ignore_index=True)
        frame["open_time"] = pd.to_datetime(frame["open_time"], errors="coerce", utc=True)
        frame = frame.dropna(subset=["open_time"]).reset_index(drop=True)
        frame["distance_to_ema_pct"] = pd.to_numeric(frame["distance_to_ema_pct"], errors="coerce").fillna(0.0)
        frame["future_return_20"] = pd.to_numeric(frame["future_return_20"], errors="coerce").fillna(0.0)
        frame["drawdown"] = pd.to_numeric(frame["drawdown"], errors="coerce").fillna(0.0)
        return frame

    def _run_fidelity_audit(
        self,
        events: pd.DataFrame,
        strategy: TradeOutcomeNextGenV1Strategy,
        cfg: TradeOutcomeControlledImplementationConfig,
    ) -> dict[str, Any]:
        lab_mask = pd.to_numeric(events["distance_to_ema_pct"], errors="coerce").fillna(0.0) <= float(cfg.distance_threshold)
        strategy_mask = strategy.event_entry_mask(events)

        tp = int((lab_mask & strategy_mask).sum())
        fp = int((~lab_mask & strategy_mask).sum())
        fn = int((lab_mask & ~strategy_mask).sum())

        precision = tp / max(1, (tp + fp))
        recall = tp / max(1, (tp + fn))
        f1 = 0.0 if (precision + recall) <= 0 else (2.0 * precision * recall / (precision + recall))

        return {
            "events_total": int(len(events)),
            "lab_approved_events": int(lab_mask.sum()),
            "strategy_signals": int(strategy_mask.sum()),
            "intersection": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": float(precision),
            "recall": float(recall),
            "f1": float(f1),
            "coverage": float(strategy_mask.sum() / max(1, len(events))),
            "passes_threshold": bool(f1 >= cfg.fidelity_min_f1),
            "target_f1": float(cfg.fidelity_min_f1),
        }

    def _run_event_backtest(
        self,
        events: pd.DataFrame,
        strategy: TradeOutcomeNextGenV1Strategy,
        candidate: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mask = strategy.event_entry_mask(events)
        selected = events[mask].copy()
        returns = pd.to_numeric(selected["future_return_20"], errors="coerce").fillna(0.0)

        observed = {
            "trades": int(len(selected)),
            "profit_factor": float(profit_factor_from_pnl(returns)),
            "sharpe": float(sharpe_from_pnl(returns)),
            "expectancy": float(expectancy_from_pnl(returns)),
            "drawdown": float(max_drawdown_from_pnl(returns)),
            "win_rate": float(win_rate_from_pnl(returns)),
        }

        expected = {
            "profit_factor": float(candidate.get("expected_profit_factor", 0.0)),
            "sharpe": float(candidate.get("expected_sharpe", 0.0)),
            "expectancy": float(candidate.get("expected_expectancy", 0.0)),
            "drawdown": float(candidate.get("expected_drawdown", 0.0)),
        }

        degradation = {
            "profit_factor_pct": self._pct_delta(expected["profit_factor"], observed["profit_factor"]),
            "sharpe_pct": self._pct_delta(expected["sharpe"], observed["sharpe"]),
            "expectancy_pct": self._pct_delta(expected["expectancy"], observed["expectancy"]),
            "drawdown_pct": self._pct_delta(expected["drawdown"], observed["drawdown"]),
        }

        return observed, {"expected": expected, "observed": observed, "degradation_pct": degradation}

    def _run_optimizer_and_validation(self, cfg: TradeOutcomeControlledImplementationConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        symbol = settings.trading.default_symbol
        timeframe = settings.trading.default_timeframe
        train_start, train_end, val_start, val_end = self._validation_window(symbol, timeframe)

        history_listener = HistoryListener(checkpoint_interval=settings.optimizer.checkpoint_interval)
        metrics_listener = MetricsListener()
        event_bus = EventBus(listeners=[history_listener, LogListener(), metrics_listener], async_dispatch=False)

        execution_id = HistoryPersistenceService.new_execution_id()
        optimizer = StrategyOptimizer(event_bus=event_bus, checkpoint_interval=settings.optimizer.checkpoint_interval)
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=symbol,
                timeframe=timeframe,
                start=train_start,
                end=val_end,
                capital=float(cfg.optimizer_capital),
                top_n=10,
                workers=max(1, int(cfg.optimizer_workers)),
                max_combinations=max(1, int(cfg.optimizer_max_combinations)),
                diagnostic=False,
                execution_id=execution_id,
                strategy_name=cfg.strategy_name,
                strategy_version="v1",
                git_commit=None,
                host=None,
                cpu=None,
                python_version=None,
            )
        )

        candidate_limit = max(1, min(20, len(summary.top_results)))
        optimization_results: list[OptimizationResult] = list(summary.top_results[:candidate_limit])

        validator = OptimizationValidator(
            ValidationCriteria(
                min_trades=settings.validation.min_trades,
                min_profit_factor=settings.validation.min_profit_factor,
                max_drawdown_pct=settings.validation.max_drawdown_pct,
                min_win_rate_pct=settings.validation.min_win_rate_pct,
                min_expectancy=settings.validation.min_expectancy,
                min_sharpe=settings.validation.min_sharpe,
            ),
            strategy_name=cfg.strategy_name,
        )

        validation = validator.validate(
            optimization_results=optimization_results,
            symbol=symbol,
            timeframe=timeframe,
            capital=float(cfg.optimizer_capital),
            train_start=train_start,
            train_end=train_end,
            validation_start=val_start,
            validation_end=val_end,
            top_n=10,
        )

        with get_session() as db_session:
            history = HistoryPersistenceService(db_session)
            history.save_validation_run(
                execution_id=HistoryPersistenceService.new_execution_id(),
                optimizer_run=execution_id,
                total_tested=validation.total_candidates,
                approved=validation.passed,
                rejected=validation.discarded,
                min_profit_factor=settings.validation.min_profit_factor,
                min_trades=settings.validation.min_trades,
                max_drawdown=settings.validation.max_drawdown_pct,
                validation_status="completed",
            )

        optimizer_summary = {
            "execution_id": execution_id,
            "tested": int(summary.combinations_tested),
            "discarded": int(summary.combinations_discarded),
            "duration_seconds": float(summary.duration_seconds),
            "output_files": list(summary.output_files),
            "metrics_state": metrics_listener.state,
        }
        validation_summary = {
            "total_candidates": int(validation.total_candidates),
            "approved": int(validation.passed),
            "rejected": int(validation.discarded),
            "output_files": list(validation.output_files),
            "best_validated": None if validation.best_validated is None else {
                "parameters": validation.best_validated.parameters,
                "validation_metrics": validation.best_validated.validation_metrics,
                "overfitting_risk": validation.best_validated.overfitting_risk,
            },
        }
        return optimizer_summary, validation_summary

    def _run_research_labs(self, cfg: TradeOutcomeControlledImplementationConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        symbol = settings.trading.default_symbol
        timeframe = settings.trading.default_timeframe

        strategy_lab = StrategyResearchLab(session=self._session, base_dir=self._base_dir)
        strategy_result = strategy_lab.run(
            ResearchLabConfig(
                strategies=[cfg.strategy_name],
                symbol=symbol,
                timeframe=timeframe,
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2024, 12, 31, tzinfo=timezone.utc),
                horizon_bars=12,
                max_candidates_per_strategy=8,
            )
        )

        operations_csv = strategy_result.get("outputs", {}).get("operations_csv")
        trade_lab = TradeManagementResearchLab(session=self._session, base_dir=self._base_dir)
        trade_result = trade_lab.run(
            TradeManagementLabConfig(
                operations_csv=operations_csv,
                symbol=symbol,
                timeframe=timeframe,
                max_bars=96,
                atr_period=14,
                atr_mult=2.0,
                time_stop_bars=24,
                momentum_fast=8,
                momentum_slow=21,
                mfe_pullback_ratio=0.35,
                bootstrap_iterations=300,
            )
        )

        return strategy_result.get("summary", {}), trade_result.get("summary", {})

    def _validation_window(self, symbol: str, timeframe: str) -> tuple[datetime, datetime, datetime, datetime]:
        min_dt, max_dt = OptimizationValidator._get_available_date_range(symbol, timeframe)
        if min_dt is None or max_dt is None:
            raise ValueError(f"No candles available for validation window: {symbol}/{timeframe}")

        total = max_dt - min_dt
        if total <= timedelta(days=10):
            train_start = min_dt
            train_end = min_dt + total * 0.6
            val_start = train_end + timedelta(minutes=1)
            val_end = max_dt
            return train_start, train_end, val_start, val_end

        train_start = min_dt
        train_end = min_dt + total * 0.7
        val_start = train_end + timedelta(minutes=1)
        val_end = max_dt
        return train_start, train_end, val_start, val_end

    def _decide(self, fidelity: dict[str, Any], degradation: dict[str, Any], validation_summary: dict[str, Any]) -> str:
        if float(fidelity.get("f1", 0.0)) < 0.95:
            return "OPCAO_B"

        deg = degradation.get("degradation_pct", {})
        pf_ok = float(deg.get("profit_factor_pct", 0.0)) >= -30.0
        sh_ok = float(deg.get("sharpe_pct", 0.0)) >= -35.0
        ex_ok = float(deg.get("expectancy_pct", 0.0)) >= -35.0
        val_ok = int(validation_summary.get("approved", 0)) > 0 if validation_summary else True

        if pf_ok and sh_ok and ex_ok and val_ok:
            return "OPCAO_A"
        return "OPCAO_B"

    def _build_report(
        self,
        *,
        cfg: TradeOutcomeControlledImplementationConfig,
        started: datetime,
        decision: str,
        candidate: dict[str, Any],
        fidelity: dict[str, Any],
        backtest_observed: dict[str, Any],
        degradation: dict[str, Any],
        optimizer_summary: dict[str, Any],
        validation_summary: dict[str, Any],
        strategy_lab_summary: dict[str, Any],
        trade_lab_summary: dict[str, Any],
        notes: list[str],
    ) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": "FASE 9 - Implementacao Controlada do Candidato Aprovado",
            "status": "COMPLETED",
            "strategy": {
                "name": cfg.strategy_name,
                "target": cfg.target_name,
                "rule": cfg.approved_rule,
                "distance_threshold": cfg.distance_threshold,
            },
            "candidate_reference": candidate,
            "specification": {
                "entry": "BUY when distance_to_ema_pct <= 0.162026",
                "exit": "Managed by engine (stop/take/trailing and explicit strategy HOLD)",
                "stop": "RiskManager default_stop_loss_pct when strategy does not set explicit stop",
                "take_profit": "RiskManager default_take_profit_pct when strategy does not set explicit take",
                "risk_management": "RiskManager + PositionSizer + stake cap from settings",
                "operational_horizon": "Event horizon aligned with future_return_20 for phase comparison",
                "filters": ["distance_to_ema_pct<=0.162026"],
            },
            "fidelity_audit": fidelity,
            "backtest_comparison": degradation,
            "optimizer": optimizer_summary,
            "validation": validation_summary,
            "strategy_research_lab": strategy_lab_summary,
            "trade_management_lab": trade_lab_summary,
            "decision": decision,
            "notes": notes,
            "elapsed_seconds": float((datetime.now(timezone.utc) - started).total_seconds()),
        }

    def _write_outputs(self, report: dict[str, Any], output_prefix: str) -> dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / f"{output_prefix}_{ts}.json"
        csv_path = out_dir / f"{output_prefix}_{ts}.csv"
        md_path = out_dir / f"{output_prefix}_{ts}.md"

        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

        row = {
            "decision": report.get("decision"),
            "strategy": report.get("strategy", {}).get("name"),
            "target": report.get("strategy", {}).get("target"),
            "rule": report.get("strategy", {}).get("rule"),
            "fidelity_precision": report.get("fidelity_audit", {}).get("precision"),
            "fidelity_recall": report.get("fidelity_audit", {}).get("recall"),
            "fidelity_f1": report.get("fidelity_audit", {}).get("f1"),
            "fidelity_intersection": report.get("fidelity_audit", {}).get("intersection"),
            "false_positives": report.get("fidelity_audit", {}).get("false_positives"),
            "false_negatives": report.get("fidelity_audit", {}).get("false_negatives"),
            "observed_profit_factor": report.get("backtest_comparison", {}).get("observed", {}).get("profit_factor"),
            "expected_profit_factor": report.get("backtest_comparison", {}).get("expected", {}).get("profit_factor"),
            "degradation_profit_factor_pct": report.get("backtest_comparison", {}).get("degradation_pct", {}).get("profit_factor_pct"),
            "observed_sharpe": report.get("backtest_comparison", {}).get("observed", {}).get("sharpe"),
            "expected_sharpe": report.get("backtest_comparison", {}).get("expected", {}).get("sharpe"),
            "degradation_sharpe_pct": report.get("backtest_comparison", {}).get("degradation_pct", {}).get("sharpe_pct"),
            "observed_expectancy": report.get("backtest_comparison", {}).get("observed", {}).get("expectancy"),
            "expected_expectancy": report.get("backtest_comparison", {}).get("expected", {}).get("expectancy"),
            "degradation_expectancy_pct": report.get("backtest_comparison", {}).get("degradation_pct", {}).get("expectancy_pct"),
            "validation_approved": report.get("validation", {}).get("approved"),
        }
        pd.DataFrame([row]).to_csv(csv_path, index=False)

        lines = [
            "# FASE 9 - Implementacao Controlada",
            "",
            "## Estrategia implementada",
            f"- strategy: {row['strategy']}",
            f"- target: {row['target']}",
            f"- rule: {row['rule']}",
            "",
            "## Auditoria de fidelidade",
            f"- Precision: {float(row['fidelity_precision'] or 0.0):.6f}",
            f"- Recall: {float(row['fidelity_recall'] or 0.0):.6f}",
            f"- F1: {float(row['fidelity_f1'] or 0.0):.6f}",
            f"- Intersection: {int(row['fidelity_intersection'] or 0)}",
            f"- False Positives: {int(row['false_positives'] or 0)}",
            f"- False Negatives: {int(row['false_negatives'] or 0)}",
            "",
            "## Comparacao esperado x observado",
            f"- Profit Factor: esperado={float(row['expected_profit_factor'] or 0.0):.6f} observado={float(row['observed_profit_factor'] or 0.0):.6f} degradacao={float(row['degradation_profit_factor_pct'] or 0.0):.2f}%",
            f"- Sharpe: esperado={float(row['expected_sharpe'] or 0.0):.6f} observado={float(row['observed_sharpe'] or 0.0):.6f} degradacao={float(row['degradation_sharpe_pct'] or 0.0):.2f}%",
            f"- Expectancy: esperada={float(row['expected_expectancy'] or 0.0):.6f} observada={float(row['observed_expectancy'] or 0.0):.6f} degradacao={float(row['degradation_expectancy_pct'] or 0.0):.2f}%",
            "",
            "## Validation",
            f"- approved: {report.get('validation', {}).get('approved', 0)}",
            "",
            "## Decisao",
            f"- {report.get('decision')}",
        ]
        md_path.write_text("\n".join(lines), encoding="utf-8")

        return {
            "json": str(json_path),
            "csv": str(csv_path),
            "md": str(md_path),
        }

    def _persist(
        self,
        *,
        run_id: str,
        cfg: TradeOutcomeControlledImplementationConfig,
        decision: str,
        candidate: dict[str, Any],
        fidelity: dict[str, Any],
        report: dict[str, Any],
        outputs: dict[str, str],
    ) -> None:
        if not cfg.persist_to_db:
            return

        repo = TradeOutcomeImplementationRunRepository(self._session)
        repo.save(
            TradeOutcomeImplementationRun(
                run_id=run_id,
                status="completed",
                decision=decision,
                strategy_name=cfg.strategy_name,
                target_name=cfg.target_name,
                rule_text=cfg.approved_rule,
                fidelity_precision=float(fidelity.get("precision", 0.0)),
                fidelity_recall=float(fidelity.get("recall", 0.0)),
                fidelity_f1=float(fidelity.get("f1", 0.0)),
                overlap_count=int(fidelity.get("intersection", 0)),
                false_positives=int(fidelity.get("false_positives", 0)),
                false_negatives=int(fidelity.get("false_negatives", 0)),
                expected_profit_factor=float(candidate.get("expected_profit_factor", 0.0)),
                observed_profit_factor=float(report.get("backtest_comparison", {}).get("observed", {}).get("profit_factor", 0.0)),
                expected_sharpe=float(candidate.get("expected_sharpe", 0.0)),
                observed_sharpe=float(report.get("backtest_comparison", {}).get("observed", {}).get("sharpe", 0.0)),
                expected_expectancy=float(candidate.get("expected_expectancy", 0.0)),
                observed_expectancy=float(report.get("backtest_comparison", {}).get("observed", {}).get("expectancy", 0.0)),
                expected_drawdown=float(candidate.get("expected_drawdown", 0.0)),
                observed_drawdown=float(report.get("backtest_comparison", {}).get("observed", {}).get("drawdown", 0.0)),
                artifacts_json=json.dumps(outputs, ensure_ascii=True),
                summary_json=json.dumps(report, ensure_ascii=True),
            )
        )

    @staticmethod
    def _pct_delta(expected: float, observed: float) -> float:
        if abs(expected) < 1e-12:
            return 0.0 if abs(observed) < 1e-12 else 100.0
        return ((observed - expected) / abs(expected)) * 100.0
