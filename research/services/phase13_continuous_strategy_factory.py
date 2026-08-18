from __future__ import annotations

from collections import deque
import csv
import json
import os
import platform
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from config.settings import settings
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.repositories import CandleRepository
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService
from paper_trading.paper_trader import PaperTrader
from risk.risk_manager import RiskManager
from strategies.factory import create_strategy
from strategies.registry import list_registered_strategies
from utils.logger import get_logger
from validation.validator import OptimizationValidator, ValidationCriteria, default_validation_window

logger = get_logger(__name__)


@dataclass(frozen=True)
class Phase13ContinuousFactoryConfig:
    symbol: str
    timeframe: str
    window_days: int = 120
    capital: float = 10_000.0
    batch_size: int = 20
    target_approved: int = 3
    target_paper_candidates: int = 3
    max_bars: int = 3500
    optimizer_max_combinations: int = 15
    optimizer_workers: int = 1
    max_strategy_runtime_seconds: int = 900
    max_cpu_per_worker_pct: float = 100.0
    campaign_max_seconds: int = 0
    campaign_end_hour: int = 9
    auto_research_when_queue_empty: bool = True
    phase14_top_n: int = 30
    stop_on_target_paper_candidates: bool = False
    checkpoint_interval_seconds: int = 600
    optimizer_probe_enabled: bool = True
    probe_max_combinations: int = 8
    probe_top_n: int = 3
    reprocess_implemented_catalog: bool = True
    paper_candidate_min_trades: int = 100
    paper_candidate_min_profit_factor: float = 1.10
    paper_candidate_min_expectancy: float = 0.0
    paper_candidate_allow_overfitting: bool = False
    paper_experiment_max_cycles: int = 1
    paper_experiment_poll_seconds: float = 2.0
    paper_experiment_bootstrap_bars: int = 1500
    paper_experiment_bootstrap_replay_bars: int = 350
    paper_experiment_review_window_days: int = 14
    output_prefix: str = "phase13_continuous_strategy_factory"


class ContinuousStrategyFactoryService:
    """FASE 13 experimental orchestrator that reuses existing scientific modules."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._phase13_6_focus_candidates = {
            self._canon("ClassicEMACrossover"),
            self._canon("ClassicSMACrossover"),
            self._canon("ClassicATRBreakout"),
            self._canon("ClassicDonchianBreakout"),
        }

    def run(self, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        run_id = HistoryPersistenceService.new_execution_id()
        started_at = datetime.now(tz=timezone.utc)
        campaign_t0 = perf_counter()
        state = self._load_state()

        # Campaign must run until next configured hour (default 09:00 local time).
        local_now = datetime.now()
        deadline_local = local_now.replace(hour=int(cfg.campaign_end_hour), minute=0, second=0, microsecond=0)
        if local_now >= deadline_local:
            deadline_local = deadline_local + timedelta(days=1)

        backlog = self._build_or_refresh_backlog(state)
        if not backlog:
            report = {
                "phase": "13",
                "run_id": run_id,
                "generated_at": started_at.isoformat(),
                "status": "no_backlog",
                "message": "No strategies available in ranking/backlog.",
                "backlog": [],
            }
            outputs = self._write_outputs(cfg.output_prefix, report)
            self._persist_checkpoint(run_id, report, cfg=cfg, report=report, final=True)
            return {"summary": report, "report": report, "outputs": outputs}

        start_dt, end_dt = self._resolve_window(cfg.window_days)
        
        # Priority logic: separate IMPLEMENTATION_PENDING/INCOMPLETE from others
        pending_items = [b for b in backlog if self._is_queue_eligible(b) and b.get("state") in ["IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"]]
        other_items = [b for b in backlog if self._is_queue_eligible(b) and b.get("state") not in ["IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"]]
        
        # Priority queue: process pending first, then others
        eligible = pending_items + other_items
        queue = deque(eligible)

        coverage_before = self._coverage_snapshot(backlog)
        processed: list[dict[str, Any]] = []
        stage_counters = {
            "reprocessed_total": 0,
            "implementation_real": 0,
            "backtest_reached": 0,
            "optimizer_probe_reached": 0,
            "optimizer_reached": 0,
            "validation_reached": 0,
            "paper_qualification_reached": 0,
        }

        # For the initial checkpoint, processed = backlog items not in queue (terminal states).
        # This satisfies: processed + pending == total (len(backlog)).
        _initial_non_queue = len(backlog) - len(queue)
        self._persist_checkpoint(
            run_id,
            {
                "phase": "13",
                "status": "running",
                "started_at": started_at.isoformat(),
                "campaign_seconds": 0.0,
                "processed": _initial_non_queue,
                "total_strategies": len(backlog),
                "approved": self._approved_count(backlog),
                "paper_candidates": len([x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]),
                "pending": len(queue),
                "deadline_local": deadline_local.isoformat(),
            },
            cfg=cfg,
        )
        
        # Track rejection knowledge for learning
        rejection_knowledge = {
            "family": {},
            "indicators": {},
            "stage": {},
            "all_rejections": [],
        }

        stop_reason = "backlog_empty"
        max_strategies_budget = max(1, int(cfg.batch_size))
        strategy_in_progress = None

        next_checkpoint_due = perf_counter() + max(60, int(cfg.checkpoint_interval_seconds))
        research_cycles = 0
        phase14_researched_total = 0
        phase14_added_total = 0

        while True:
            if datetime.now() >= deadline_local and not strategy_in_progress:
                stop_reason = "campaign_end_hour_reached"
                break

            # Check budget (but don't stop if strategy is in progress)
            if len(processed) >= max_strategies_budget and not strategy_in_progress:
                stop_reason = "budget_max_strategies"
                break

            if int(cfg.campaign_max_seconds) > 0 and (perf_counter() - campaign_t0) >= int(cfg.campaign_max_seconds) and not strategy_in_progress:
                stop_reason = "budget_time_limit"
                break

            if not queue:
                if strategy_in_progress:
                    break

                # No local work: optionally trigger Phase 14 and rebuild backlog.
                if bool(cfg.auto_research_when_queue_empty):
                    phase14_report = self._run_phase14_research(top_n=int(cfg.phase14_top_n))
                    research_cycles += 1
                    phase14_researched_total += int(phase14_report.get("total_researched", 0))
                    phase14_added_total += int(phase14_report.get("total_classified", 0))

                    self._save_state(backlog)
                    refreshed_backlog = self._build_or_refresh_backlog(self._load_state())
                    backlog.clear()
                    backlog.extend(refreshed_backlog)
                    eligible = self._build_active_queue(backlog, cfg)
                    queue = deque(eligible)

                    if not queue:
                        stop_reason = "backlog_and_sources_exhausted"
                        break
                    continue

                stop_reason = "no_more_pending_strategies"
                break
            
            item = queue.popleft()
            strategy_in_progress = item
            self._stage_log(item, "selected", "started", "strategy selected from active queue")
            item_t0 = perf_counter()

            try:
                self._process_queue_item(
                    item=item,
                    cfg=cfg,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    stage_counters=stage_counters,
                )
                # Record learning data for rejections
                if item.get("state") in ["REJECTED_BY_PERFORMANCE", "REJECTED_BY_INFRASTRUCTURE"]:
                    self._record_rejection_knowledge(item, rejection_knowledge)
            except (KeyboardInterrupt, SystemExit) as e:
                logger.warning(
                    "FASE13 — campaign interrupted (%s) during strategy %s — saving state before exit",
                    type(e).__name__, item.get("candidate_name"),
                )
                item["state"] = "ERROR_RESILIENCE_CONTINUED"
                item["state_reason"] = f"interrupted:{type(e).__name__}"
                self._stage_log(item, "error", "interrupted", type(e).__name__)
                strategy_in_progress = None
                item["processing_seconds"] = round(perf_counter() - item_t0, 3)
                item["last_processed_run_id"] = run_id
                processed.append(item)
                self._save_state(backlog)
                self._persist_checkpoint(
                    run_id,
                    {
                        "phase": "13",
                        "status": "interrupted",
                        "started_at": started_at.isoformat(),
                        "campaign_seconds": round(perf_counter() - campaign_t0, 3),
                        "processed": _initial_non_queue + len(processed),
                        "total_strategies": len(backlog),
                        "approved": self._approved_count(backlog),
                        "paper_candidates": len([x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]),
                        "pending": len(queue),
                        "interrupted_strategy": item.get("candidate_name"),
                    },
                    cfg=cfg,
                )
                stop_reason = "interrupted"
                break
            except Exception as e:
                logger.warning(f"Strategy {item.get('candidate_name')} failed: {e}, continuing...")
                item["state"] = "ERROR_RESILIENCE_CONTINUED"
                self._stage_log(item, "error", "continued", str(e))
            
            strategy_in_progress = None
            stage_counters["reprocessed_total"] = stage_counters.get("reprocessed_total", 0) + 1
            item["processing_seconds"] = round(perf_counter() - item_t0, 3)
            item["last_processed_run_id"] = run_id
            processed.append(item)

            if perf_counter() >= next_checkpoint_due:
                self._persist_checkpoint(
                    run_id,
                    {
                        "phase": "13",
                        "status": "running",
                        "started_at": started_at.isoformat(),
                        "campaign_seconds": round(perf_counter() - campaign_t0, 3),
                        "processed": _initial_non_queue + len(processed),
                        "total_strategies": len(backlog),
                        "approved": self._approved_count(backlog),
                        "paper_candidates": len([x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]),
                        "pending": len(queue),
                        "deadline_local": deadline_local.isoformat(),
                    },
                    cfg=cfg,
                )
                next_checkpoint_due = perf_counter() + max(60, int(cfg.checkpoint_interval_seconds))

        if queue == deque() and stop_reason == "backlog_empty":
            stop_reason = "no_more_pending_strategies"

        coverage_after = self._coverage_snapshot(backlog)
        report = self._build_report(
            run_id=run_id,
            started_at=started_at,
            cfg=cfg,
            backlog=backlog,
            processed=processed,
            stop_reason=stop_reason,
            campaign_seconds=round(perf_counter() - campaign_t0, 3),
            stage_counters=stage_counters,
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            rejection_knowledge=rejection_knowledge,
        )
        report["overnight_v2"] = {
            "deadline_local": deadline_local.isoformat(),
            "research_cycles": int(research_cycles),
            "phase14_researched_total": int(phase14_researched_total),
            "phase14_added_total": int(phase14_added_total),
            "paper_candidate_should_auto_start_paper_experimental": "SIM",
            "paper_candidate_promotion_rule_audit": self._paper_candidate_promotion_audit(backlog),
        }

        self._save_state(backlog)
        outputs = self._write_outputs(cfg.output_prefix, report)
        self._persist_checkpoint(run_id, report, cfg=cfg, report=report, final=True)

        summary = {
            "status": "completed",
            "phase": "13",
            "run_id": run_id,
            "implemented": int(report.get("implemented_count", 0)),
            "rejected": int(report.get("rejected_count", 0)),
            "approved": int(report.get("approved_count", 0)),
            "in_paper_trading": int(report.get("in_paper_trading_count", 0)),
            "paper_candidates_found": len([x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]),
            "target_paper_candidates": int(cfg.target_paper_candidates),
            "stop_reason": stop_reason,
            "research_cycles": int(research_cycles),
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _run_phase14_research(self, top_n: int) -> dict[str, Any]:
        """Run Phase 14 from Phase 13 loop to refill backlog automatically."""
        try:
            from research.services.phase14_market_intelligence import (
                MarketIntelligenceService,
                Phase14MarketIntelligenceConfig,
            )

            service = MarketIntelligenceService(base_dir=self._base_dir)
            result = service.run(
                Phase14MarketIntelligenceConfig(
                    top_n=max(5, int(top_n)),
                    output_prefix="phase14_overnight_v2",
                )
            )
            return result.get("report", {}) if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning("Phase 14 auto-research failed during overnight loop: %s", exc)
            return {"error": str(exc)}

    def _paper_candidate_promotion_audit(self, backlog: list[dict[str, Any]]) -> dict[str, Any]:
        """Audit consistency: PAPER_CANDIDATE should auto-start paper experimental."""
        paper_candidates = [x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]
        with_experiment = [x for x in paper_candidates if isinstance(x.get("paper_experimental"), dict)]
        without_experiment = [x for x in paper_candidates if not isinstance(x.get("paper_experimental"), dict)]
        return {
            "paper_candidate_count": len(paper_candidates),
            "paper_experimental_started_count": len(with_experiment),
            "paper_candidate_without_experimental_count": len(without_experiment),
            "paper_candidate_should_start_automatically": "SIM",
            "status": "CONSISTENT" if not without_experiment else "INCONSISTENT",
            "without_experimental": [x.get("candidate_name") for x in without_experiment],
        }

    def _is_queue_eligible(self, item: dict[str, Any]) -> bool:
        state = str(item.get("state", ""))
        if state == "REJECTED_BY_INFRASTRUCTURE":
            return self._is_rr_mismatch_infrastructure(item)

        return state in {
            "researched",
            "queued",
            "awaiting_evaluation",
            "implemented",
            "IMPLEMENTATION_PENDING",
            "IMPLEMENTATION_INCOMPLETE",
            "INCONCLUSIVE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
            "INCONCLUSIVE_LOW_SAMPLE",
            "NO_TRADES",
            "REJECTED_BY_PERFORMANCE",
            "REJECTED",
            "REJECTED_AFTER_PAPER_EXPERIMENT",
        }

    def _is_rr_mismatch_infrastructure(self, item: dict[str, Any]) -> bool:
        """Return True only for infra rejections that are RR-threshold mismatches."""
        reason = str(item.get("rejection_reason") or item.get("state_reason") or "")
        return (
            "risk_error:" in reason
            and "Risk/reward ratio" in reason
            and "below the configured minimum" in reason
        )

    def _build_active_queue(
        self,
        backlog: list[dict[str, Any]],
        cfg: Phase13ContinuousFactoryConfig,
    ) -> list[dict[str, Any]]:
        ordered = sorted(backlog, key=lambda x: float(x.get("queue_score", 0.0)), reverse=True)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in ordered:
            key = self._canon(str(item.get("candidate_name", "")))
            if not key or key in seen:
                continue

            state = str(item.get("state", ""))
            has_impl = bool(item.get("platform_strategy_name"))
            infra_failed = state in {"REJECTED_BY_INFRASTRUCTURE", "SMOKE_FAILED"}
            rr_mismatch_infra = state == "REJECTED_BY_INFRASTRUCTURE" and self._is_rr_mismatch_infrastructure(item)

            if bool(cfg.reprocess_implemented_catalog) and has_impl and ((not infra_failed) or rr_mismatch_infra):
                if state in {
                    "REJECTED_BY_PERFORMANCE",
                    "REJECTED_BY_INFRASTRUCTURE",
                    "queued",
                    "implemented",
                    "awaiting_evaluation",
                    "INCONCLUSIVE",
                    "INCONCLUSIVE_RESOURCE_LIMIT",
                    "INCONCLUSIVE_LOW_SAMPLE",
                    "NO_TRADES",
                }:
                    out.append(item)
                    seen.add(key)
                    continue

            if self._is_queue_eligible(item):
                out.append(item)
                seen.add(key)

        return out

    def _stage_log(self, item: dict[str, Any], stage: str, status: str, detail: str) -> None:
        ts = datetime.now(tz=timezone.utc).isoformat()
        item.setdefault("history", []).append(
            {
                "ts": ts,
                "event": f"{stage}:{status}",
                "detail": detail,
            }
        )
        logger.info("FASE13 strategy=%s stage=%s status=%s detail=%s", item.get("candidate_name"), stage, status, detail)

    def _process_queue_item(
        self,
        item: dict[str, Any],
        cfg: Phase13ContinuousFactoryConfig,
        start_dt: datetime,
        end_dt: datetime,
        stage_counters: dict[str, int],
    ) -> None:
        item["state"] = "IMPLEMENTATION_IN_PROGRESS"
        item["last_run_id"] = item.get("last_run_id") or HistoryPersistenceService.new_execution_id()
        item["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        t0 = perf_counter()

        self._stage_log(item, "implementation", "started", "automatic implementation attempt")
        auto_impl = self._auto_implement_candidate(item)
        item["implementation"] = auto_impl
        strategy_name = str(item.get("platform_strategy_name") or "")
        if not auto_impl.get("implemented", False) or not strategy_name:
            item["state"] = "IMPLEMENTATION_PENDING"
            item["state_reason"] = str(auto_impl.get("reason", "NOT_IMPLEMENTED_IN_PLATFORM"))
            item["rejection_stage"] = "implementation"
            item["rejection_reason"] = item["state_reason"]
            self._stage_log(item, "implementation", "failed", item["state_reason"])
            return

        item["state"] = "IMPLEMENTED"
        stage_counters["implementation_real"] = stage_counters.get("implementation_real", 0) + 1
        self._stage_log(item, "implementation", "completed", f"platform_strategy={strategy_name}")

        if self._strategy_budget_exhausted(item, cfg, t0):
            return

        self._stage_log(item, "smoke", "started", "running smoke test")
        market_df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt, cfg.max_bars)
        if market_df.empty:
            item["state"] = "REJECTED_BY_INFRASTRUCTURE"
            item["state_reason"] = "no_market_data"
            item["rejection_stage"] = "smoke_test"
            item["rejection_reason"] = "no_market_data"
            self._stage_log(item, "smoke", "failed", "no_market_data")
            self._register_rejection_knowledge(item, stage="smoke_test", metrics={})
            return

        smoke = self._run_smoke(strategy_name, market_df, cfg)
        item["smoke"] = smoke
        if not smoke.get("passed", False):
            reason = ";".join(smoke.get("errors", [])) or "smoke_failed"
            item["state"] = "REJECTED_BY_INFRASTRUCTURE"
            item["state_reason"] = reason
            item["rejection_stage"] = "smoke_test"
            item["rejection_reason"] = reason
            self._stage_log(item, "smoke", "failed", reason)
            self._register_rejection_knowledge(item, stage="smoke_test", metrics={})
            return
        self._stage_log(item, "smoke", "completed", "passed")

        if self._strategy_budget_exhausted(item, cfg, t0):
            return

        self._stage_log(item, "backtest", "started", "running backtest")
        backtest = self._run_backtest(strategy_name, market_df, cfg)
        item["backtest_base"] = backtest
        item["backtest"] = backtest
        baseline = self._run_baseline_backtest(market_df, cfg)
        item["baseline"] = baseline
        item["baseline_comparison"] = self._compare_to_baseline(backtest, baseline)
        stage_counters["backtest_reached"] = stage_counters.get("backtest_reached", 0) + 1
        self._stage_log(item, "backtest", "completed", f"trades={int(backtest.get('number_of_trades', 0))}")

        old_early_stop = self._early_stop(backtest)
        item["early_stop_old_pipeline"] = old_early_stop

        probe_report: dict[str, Any] | None = None
        probe_best_metrics: dict[str, Any] | None = None
        comparison = {
            "old_pipeline": {
                "metrics": dict(backtest),
                "eliminated": "SIM" if old_early_stop.get("triggered", False) else "NAO",
                "early_stop_reasons": list(old_early_stop.get("reasons", [])),
            },
            "new_pipeline": {},
        }

        if bool(cfg.optimizer_probe_enabled):
            self._stage_log(item, "optimizer_probe", "started", "running low-cost optimizer probe")
            probe_report = self._run_optimizer_probe(strategy_name, cfg, start_dt, end_dt)
            item["optimizer_probe"] = probe_report
            stage_counters["optimizer_probe_reached"] = stage_counters.get("optimizer_probe_reached", 0) + 1
            probe_best_metrics = self._extract_probe_best_metrics(probe_report)
            if probe_best_metrics:
                item["probe_reassessment"] = {
                    "before": dict(backtest),
                    "after": dict(probe_best_metrics),
                    "best_combination": dict(probe_report.get("best_parameters", {})),
                    "metric_deltas_pct": self._metrics_delta_pct(backtest, probe_best_metrics),
                }
                self._stage_log(
                    item,
                    "optimizer_probe",
                    "completed",
                    f"best_rank={probe_report.get('best_rank')} combinations={probe_report.get('combinations_tested', 0)}",
                )
            else:
                self._stage_log(item, "optimizer_probe", "completed", "no_probe_best_metrics")

        reevaluated_metrics = probe_best_metrics if probe_best_metrics else backtest
        early_stop = self._early_stop(reevaluated_metrics)
        item["early_stop"] = early_stop
        item["early_stop_input"] = {
            "source": "optimizer_probe" if probe_best_metrics else "backtest_base",
            "metrics": dict(reevaluated_metrics),
        }

        comparison["new_pipeline"] = {
            "metrics": dict(reevaluated_metrics),
            "eliminated": "SIM" if early_stop.get("triggered", False) else "NAO",
            "early_stop_reasons": list(early_stop.get("reasons", [])),
            "probe_used": "SIM" if probe_best_metrics else "NAO",
        }
        item["pipeline_comparison"] = comparison

        if early_stop.get("triggered", False):
            reason = ";".join(early_stop.get("reasons", [])) or "early_stop"
            classification = str(early_stop.get("classification", "REJECTED_BY_PERFORMANCE"))
            if classification == "NO_TRADES":
                item["state"] = "NO_TRADES"
            elif classification == "INCONCLUSIVE_LOW_SAMPLE":
                item["state"] = "INCONCLUSIVE_LOW_SAMPLE"
            else:
                item["state"] = "REJECTED_BY_PERFORMANCE"
            item["state_reason"] = reason
            item["rejection_stage"] = "early_stop"
            item["rejection_reason"] = reason
            item["early_stop_classification"] = classification
            item["early_stop_details"] = dict(early_stop.get("details", {}))
            self._stage_log(item, "early_stop", "triggered", reason)
            if item["state"] == "REJECTED_BY_PERFORMANCE":
                self._register_rejection_knowledge(item, stage="early_stop", metrics=backtest)
            return
        self._stage_log(item, "early_stop", "passed", "eligible_for_optimizer")

        if self._strategy_budget_exhausted(item, cfg, t0):
            return

        self._stage_log(item, "optimizer", "started", "running optimizer")
        optimizer_report = self._run_optimizer(strategy_name, cfg, start_dt, end_dt)
        item["optimizer"] = optimizer_report
        stage_counters["optimizer_reached"] = stage_counters.get("optimizer_reached", 0) + 1
        self._stage_log(
            item,
            "optimizer",
            "completed",
            f"combinations={int(optimizer_report.get('combinations_tested', 0))}",
        )

        if self._strategy_budget_exhausted(item, cfg, t0):
            return

        self._stage_log(item, "validation", "started", "running validation")
        validation = self._run_validation(strategy_name, cfg, start_dt, end_dt, optimizer_report)
        item["validation"] = validation
        stage_counters["validation_reached"] = stage_counters.get("validation_reached", 0) + 1
        approved = bool(validation.get("approved", False))
        self._stage_log(item, "validation", "completed", "approved" if approved else "rejected")

        if not approved:
            candidate_eval = self._evaluate_paper_candidate(item, optimizer_report, validation, cfg)
            item["paper_candidate_evaluation"] = candidate_eval

            if bool(candidate_eval.get("eligible", False)):
                item["state"] = "PAPER_CANDIDATE"
                item["state_reason"] = "paper_candidate_threshold_reached"
                item["classification"] = "PAPER_CANDIDATE"
                self._stage_log(item, "classification", "paper_candidate", "eligible_for_paper_experimental")

                self._stage_log(item, "paper_experimental", "started", "starting paper experimental campaign")
                experiment = self._run_paper_candidate_experiment(strategy_name, cfg)
                item["paper_experimental"] = experiment

                experiment_status = str(experiment.get("status", "")).lower()
                experiment_assessment = str(experiment.get("assessment", "inconclusive")).lower()
                if experiment_status == "failed":
                    item["state"] = "REJECTED_AFTER_PAPER_EXPERIMENT"
                    item["state_reason"] = str(experiment.get("reason", "paper_experiment_failed"))
                    item["rejection_stage"] = "paper_experimental"
                    item["rejection_reason"] = item["state_reason"]
                    item["classification"] = "REJECTED"
                    self._stage_log(item, "paper_experimental", "failed", item["state_reason"])
                    self._register_rejection_knowledge(item, stage="paper_experimental", metrics=backtest)
                    return

                if experiment_assessment == "worsened":
                    item["state"] = "REJECTED_AFTER_PAPER_EXPERIMENT"
                    item["state_reason"] = "deterioration_detected_in_paper_experiment"
                    item["rejection_stage"] = "paper_experimental"
                    item["rejection_reason"] = item["state_reason"]
                    item["classification"] = "REJECTED"
                    self._stage_log(item, "paper_experimental", "completed", "worsened")
                    self._register_rejection_knowledge(item, stage="paper_experimental", metrics=backtest)
                    return

                item["classification"] = "PAPER_CANDIDATE"
                item["paper_experiment_revalidation"] = {
                    "recommended": True,
                    "reason": "submit_to_full_validation_after_experimental_window",
                    "review_window_days": int(cfg.paper_experiment_review_window_days),
                }
                self._stage_log(item, "paper_experimental", "completed", experiment_assessment)
                return

            reason = ";".join(validation.get("reasons", [])) or "validation_rejected"
            item["state"] = "REJECTED"
            item["state_reason"] = reason
            item["rejection_stage"] = "validation"
            item["rejection_reason"] = reason
            item["classification"] = "REJECTED"
            self._register_rejection_knowledge(item, stage="validation", metrics=backtest)
            return

        self._stage_log(item, "paper_qualification", "started", "running paper qualification")
        paper = self._prepare_paper_trading(strategy_name, market_df, cfg)
        item["paper_trading"] = paper
        stage_counters["paper_qualification_reached"] = stage_counters.get("paper_qualification_reached", 0) + 1

        if str(paper.get("status", "")).lower() == "running":
            item["state"] = "PAPER_APPROVED"
            item["state_reason"] = "paper_qualification_started"
            item["classification"] = "PAPER_APPROVED"
            self._stage_log(item, "paper_qualification", "completed", "running")
        else:
            item["state"] = "PAPER_APPROVED"
            item["state_reason"] = "paper_qualification_failed_to_start"
            item["classification"] = "PAPER_APPROVED"
            self._stage_log(item, "paper_qualification", "completed", "approved_without_running")

    def _evaluate_paper_candidate(
        self,
        item: dict[str, Any],
        optimizer_report: dict[str, Any],
        validation: dict[str, Any],
        cfg: Phase13ContinuousFactoryConfig,
    ) -> dict[str, Any]:
        implementation = item.get("implementation", {}) if isinstance(item.get("implementation"), dict) else {}
        implementation_faithful = bool(implementation.get("implemented", False)) and str(
            implementation.get("mode", "")
        ).lower() in {"native_exact", "native"}

        top_rows = optimizer_report.get("top_results", []) if isinstance(optimizer_report, dict) else []
        top_metrics = {}
        if top_rows and isinstance(top_rows[0], dict):
            top_metrics = dict(top_rows[0].get("metrics", {})) if isinstance(top_rows[0].get("metrics", {}), dict) else {}

        total_trades = int(top_metrics.get("total_trades", top_metrics.get("trades", 0)) or 0)
        profit_factor = float(top_metrics.get("profit_factor", 0.0) or 0.0)
        expectancy = float(top_metrics.get("expectancy", 0.0) or 0.0)

        snapshot = validation.get("snapshot", {}) if isinstance(validation.get("snapshot", {}), dict) else {}
        overfitting_risk = bool(snapshot.get("overfitting_risk", False))
        has_overfit_reason = "possible_overfitting" in str(snapshot.get("discard_reasons", ""))
        overfitting_impeditive = overfitting_risk or has_overfit_reason

        infra_ok = str(item.get("rejection_stage", "")) not in {"implementation", "smoke_test", "paper_experimental"}

        reasons: list[str] = []
        if not implementation_faithful:
            reasons.append("implementation_not_faithful")
        if not infra_ok:
            reasons.append("structural_or_infrastructure_failure")
        if total_trades < int(cfg.paper_candidate_min_trades):
            reasons.append(f"trades_below_paper_candidate_threshold:{total_trades}<{int(cfg.paper_candidate_min_trades)}")
        if profit_factor < float(cfg.paper_candidate_min_profit_factor):
            reasons.append(
                f"profit_factor_below_paper_candidate_threshold:{round(profit_factor, 4)}<{float(cfg.paper_candidate_min_profit_factor)}"
            )
        if expectancy <= float(cfg.paper_candidate_min_expectancy):
            reasons.append(
                f"expectancy_below_paper_candidate_threshold:{round(expectancy, 4)}<={float(cfg.paper_candidate_min_expectancy)}"
            )
        if overfitting_impeditive and not bool(cfg.paper_candidate_allow_overfitting):
            reasons.append("clear_overfitting_detected")

        return {
            "eligible": len(reasons) == 0,
            "criteria": {
                "implementation_faithful": implementation_faithful,
                "infra_ok": infra_ok,
                "min_trades": int(cfg.paper_candidate_min_trades),
                "min_profit_factor": float(cfg.paper_candidate_min_profit_factor),
                "min_expectancy": float(cfg.paper_candidate_min_expectancy),
                "allow_overfitting": bool(cfg.paper_candidate_allow_overfitting),
            },
            "metrics_used": {
                "total_trades": total_trades,
                "profit_factor": profit_factor,
                "expectancy": expectancy,
                "overfitting_risk": overfitting_risk,
            },
            "reasons": reasons,
        }

    def _run_paper_candidate_experiment(self, strategy_name: str, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        version_tag = f"paper_candidate_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        service = PaperLiveService(base_dir=self._base_dir)
        try:
            result = service.run(
                PaperLiveConfig(
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                    strategy_name=strategy_name,
                    strategy_version=version_tag,
                    initial_capital=max(100.0, float(cfg.capital)),
                    poll_seconds=max(1.0, float(cfg.paper_experiment_poll_seconds)),
                    bootstrap_bars=max(200, int(cfg.paper_experiment_bootstrap_bars)),
                    bootstrap_replay_bars=max(60, int(cfg.paper_experiment_bootstrap_replay_bars)),
                    max_cycles=max(1, int(cfg.paper_experiment_max_cycles)),
                    resume=False,
                    min_trades_before_change=0,
                    output_prefix=f"paper_candidate_{self._canon(strategy_name)}",
                )
            )
        except Exception as exc:
            return {
                "status": "failed",
                "reason": str(exc),
                "strategy_name": strategy_name,
            }

        comparison = result.get("version_comparison", {}) if isinstance(result.get("version_comparison", {}), dict) else {}
        assessment = str(comparison.get("status", "inconclusive")).lower()
        return {
            "status": str(result.get("status", "completed")),
            "assessment": assessment,
            "strategy_name": strategy_name,
            "strategy_version": version_tag,
            "processed_bars": int(result.get("processed_bars", 0) or 0),
            "closed_trades": int(result.get("closed_trades", 0) or 0),
            "version_comparison": comparison,
            "raw": result,
        }

    def _strategy_budget_exhausted(self, item: dict[str, Any], cfg: Phase13ContinuousFactoryConfig, t0: float) -> bool:
        elapsed = perf_counter() - t0
        if elapsed <= max(30, int(cfg.max_strategy_runtime_seconds)):
            return False
        item["state"] = "INCONCLUSIVE_RESOURCE_LIMIT"
        item["state_reason"] = "RESOURCE_LIMIT:strategy_runtime"
        item["resource_limit"] = {
            "elapsed_seconds": round(elapsed, 3),
            "max_strategy_runtime_seconds": int(cfg.max_strategy_runtime_seconds),
            "max_optimizer_combinations": int(cfg.optimizer_max_combinations),
            "max_cpu_per_worker_pct": float(cfg.max_cpu_per_worker_pct),
        }
        self._stage_log(item, "resource_limit", "triggered", "strategy_runtime")
        return True

    def _load_phase11_ranking(self) -> list[dict[str, Any]]:
        seed = self._load_phase14_seed_backlog()
        if seed:
            rows: list[dict[str, Any]] = []
            for row in seed:
                rows.append(
                    {
                        "rank": row.get("rank"),
                        "name": row.get("name"),
                        "category": row.get("category") or "unknown",
                        "priority": self._priority_label_from_classification(str(row.get("classification", ""))),
                        "score_total": row.get("market_intelligence_score", 0.0),
                        "indicators": row.get("indicators", []),
                        "recommended_timeframes": row.get("recommended_timeframes", []),
                        "supported_crypto": row.get("supported_crypto", []),
                        "source_kind": "MarketIntelligence",
                    }
                )
            return rows

        candidates = sorted(self._results_dir.glob("crypto_strategy_research_*_ranking.csv"), key=lambda p: p.stat().st_mtime)
        if candidates:
            latest = candidates[-1]
            with latest.open("r", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))

        kb_path = self._base_dir / "research" / "crypto_strategy_knowledge_base" / "strategies.json"
        if kb_path.exists():
            rows = json.loads(kb_path.read_text(encoding="utf-8"))
            enriched = []
            for idx, row in enumerate(rows, start=1):
                enriched.append(
                    {
                        "rank": idx,
                        "name": row.get("name"),
                        "category": row.get("category"),
                        "priority": row.get("priority") or "Alta",
                        "score_total": row.get("score_total") or 70.0,
                        "indicators": row.get("indicators", []),
                        "recommended_timeframes": row.get("recommended_timeframes", []),
                        "source_kind": row.get("source_kind", "Open Source"),
                    }
                )
            return enriched
        return []

    def _build_or_refresh_backlog(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        existing = {self._canon(str(item.get("candidate_name", ""))): item for item in state.get("backlog", [])}
        ranking = self._load_phase11_ranking()
        platform_map = self._platform_strategy_map()

        for row in ranking:
            candidate = str(row.get("name") or row.get("strategy") or "").strip()
            if not candidate:
                continue
            key = self._canon(candidate)
            item = existing.get(key)
            if item is None:
                platform_name = platform_map.get(key)
                indicators = row.get("indicators")
                if isinstance(indicators, str):
                    indicators = [x.strip() for x in indicators.split(",") if x.strip()]
                item = {
                    "candidate_name": candidate,
                    "platform_strategy_name": platform_name,
                    "implementation_mode": "native" if platform_name else "pending",
                    "family": self._family_from_row(row),
                    "origin": row.get("source_kind", "unknown"),
                    "priority": row.get("priority", "Alta"),
                    "score": float(row.get("score_total", 0.0) or 0.0),
                    "indicators": indicators or [],
                    "timeframes": self._coerce_list(row.get("recommended_timeframes")),
                    "assets": self._coerce_list(row.get("supported_crypto")),
                    "state": "queued" if platform_name else "IMPLEMENTATION_PENDING",
                    "created_at": datetime.now(tz=timezone.utc).isoformat(),
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                    "history": [],
                }
                existing[key] = item

            if item.get("state") == "researched" and item.get("platform_strategy_name"):
                item["state"] = "queued"
            if str(item.get("rejection_reason", "")).upper().find("NOT_IMPLEMENTED_IN_PLATFORM") >= 0:
                item["state"] = "IMPLEMENTATION_PENDING"
                item["state_reason"] = "NOT_IMPLEMENTED_IN_PLATFORM"
            if item.get("state") == "rejected" and str(item.get("rejection_stage", "")) == "implementation":
                item["state"] = "IMPLEMENTATION_PENDING"
                item["state_reason"] = str(item.get("rejection_reason", "implementation_pending"))
            item["priority_penalty"] = self._priority_penalty(item, state)
            item["queue_score"] = float(item.get("score", 0.0)) - float(item.get("priority_penalty", 0.0))
            if self._canon(str(item.get("candidate_name", ""))) in self._phase13_6_focus_candidates:
                item["queue_score"] = float(item.get("queue_score", 0.0)) + 100.0

        # Ensure existing platform strategies are always available as fallback candidates.
        for strategy in list_registered_strategies():
            strategy_name = str(strategy.get("name", "")).strip()
            if not strategy_name:
                continue
            key = self._canon(strategy_name)
            if key in existing:
                continue
            family = str(strategy.get("family", "indefinida"))
            item = {
                "candidate_name": strategy_name,
                "platform_strategy_name": strategy_name,
                "implementation_mode": "native",
                "family": family,
                "origin": "PlatformRegistry",
                "priority": "Media",
                "score": 60.0,
                "indicators": list(strategy.get("indicators", [])),
                "timeframes": [],
                "assets": ["BTC", "ETH"],
                "state": "queued",
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                "history": [],
                "priority_penalty": 0.0,
                "queue_score": 60.0,
            }
            existing[key] = item

        out = sorted(existing.values(), key=lambda x: float(x.get("queue_score", 0.0)), reverse=True)
        state["backlog"] = out
        return out

    def _platform_strategy_map(self) -> dict[str, str]:
        strategies = list_registered_strategies()
        by_name = {self._canon(str(s.get("name", ""))): str(s.get("name")) for s in strategies}
        aliases = {
            "supertrend": "SuperTrendV1",
            "supertrendv1": "SuperTrendV1",
            "tradeoutcomenextgenv1": "TradeOutcomeNextGenV1",
            "tradeoutcomenextgenv11": "TradeOutcomeNextGenV1.1",
            "trendv1": "TrendV1",
            "trendv2": "TrendV2",
            "meanreversionv1": "MeanReversionV1",
            "breakoutv1": "BreakoutV1",
            "openingrangebreakout": "BreakoutV1",
            "emaribbonpullback": "TrendV2",
            "volatilitybreakoutatr+volume": "BreakoutV1",
            "bollingerrsicryptomeanreversion": "MeanReversionV1",
            "elderimpulse": "TrendV2",
            "heikin-ashitrend+atr": "TrendV2",
            "ttmsqueezemomentum": "TradeOutcomeNextGenV1.1",
            "sessionvwapreversion": "ClassicVWAPReversion",
            "macdhistogramacceleration": "ClassicMACDTrend",
            "hullsuite": "TrendV1",
            "vidyatrend": "TrendV2",
            "kamaregimefilter": "TrendV1",
            "ichimokukumobreakout": "ClassicDonchianBreakout",
            "alphatrend": "SuperTrendV1",
            "rsi2meanreversion": "ClassicRSIMeanReversion",
            "kalmantrendfilter": "TrendV1",
            "adxtrendcontinuation": "ClassicDualMomentum",
            "atrvolatilitycompressionbreak": "ClassicATRBreakout",
        }
        out = dict(by_name)
        for key, strategy in aliases.items():
            if self._canon(strategy) in by_name:
                out[key] = strategy
        return out

    def _family_proxy_strategy(self, family: str) -> str | None:
        family_key = self._canon(family)
        proxy_by_family = {
            "tendencia": "SuperTrendV1",
            "trendfollowing": "SuperTrendV1",
            "trend": "SuperTrendV1",
            "breakout": "BreakoutV1",
            "reversao": "MeanReversionV1",
            "meanreversion": "MeanReversionV1",
            "momentum": "TrendV2",
            "volatilidade": "TradeOutcomeNextGenV1.1",
            "hibridas": "TradeOutcomeNextGenV1",
            "hibrida": "TradeOutcomeNextGenV1",
        }
        candidate = proxy_by_family.get(family_key)
        if not candidate:
            return None
        available = {self._canon(str(s.get("name", ""))): str(s.get("name")) for s in list_registered_strategies()}
        return available.get(self._canon(candidate))

    def _auto_implement_candidate(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("platform_strategy_name"):
            return {"implemented": True, "mode": "native_exact", "reason": "already_implemented"}

        candidate = str(item.get("candidate_name", ""))
        available = {self._canon(str(s.get("name", ""))): str(s.get("name")) for s in list_registered_strategies()}
        exact = available.get(self._canon(candidate))
        if exact:
            item["platform_strategy_name"] = exact
            item["implementation_mode"] = "native_exact"
            return {"implemented": True, "mode": "native_exact", "strategy": exact, "reason": "resolved_exact_name"}

        alias_map = self._platform_strategy_map()
        alias_target = alias_map.get(self._canon(candidate))
        if alias_target and self._canon(alias_target) in available:
            resolved = available[self._canon(alias_target)]
            item["platform_strategy_name"] = resolved
            item["implementation_mode"] = "native_proxy"
            return {
                "implemented": True,
                "mode": "native_proxy",
                "strategy": resolved,
                "reason": "resolved_alias_proxy",
            }

        family_proxy = self._family_proxy_strategy(str(item.get("family", "")))
        if family_proxy and self._canon(family_proxy) in available:
            resolved = available[self._canon(family_proxy)]
            item["platform_strategy_name"] = resolved
            item["implementation_mode"] = "native_proxy"
            return {
                "implemented": True,
                "mode": "native_proxy",
                "strategy": resolved,
                "reason": "resolved_family_proxy",
            }

        return {"implemented": False, "mode": "none", "reason": "NOT_IMPLEMENTED_IN_PLATFORM"}

    def _resolve_window(self, window_days: int) -> tuple[datetime, datetime]:
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(10, int(window_days)))
        return start_dt, end_dt

    def _load_df(self, symbol: str, timeframe: str, start: datetime, end: datetime, max_bars: int) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end)

        if not candles:
            return pd.DataFrame()

        frame = pd.DataFrame(
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
        return frame.tail(max(500, int(max_bars))).copy()

    def _run_smoke(self, strategy_name: str, df: pd.DataFrame, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        window = df.tail(min(len(df), 800))
        errors: list[str] = []
        signal_count = 0
        first_signal: dict[str, Any] | None = None

        try:
            strategy = create_strategy(strategy_name)
            strategy.initialize()
            for i in range(60, len(window)):
                enriched = strategy.calculate(window.iloc[: i + 1])
                signal = strategy.entry_signal(enriched)
                if str(signal.signal.value) == "BUY":
                    signal_count += 1
                    if first_signal is None:
                        first_signal = {
                            "price": float(signal.price),
                            "stop_loss": float(signal.stop_loss) if signal.stop_loss is not None else None,
                            "take_profit": float(signal.take_profit) if signal.take_profit is not None else None,
                            "score": float(signal.score),
                        }
        except Exception as exc:
            errors.append(f"signal_error:{exc}")

        risk_ok = False
        if first_signal and not errors:
            try:
                rm = RiskManager()
                strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(strategy)
                if strategy_rr_min is None:
                    strategy_rr_min = RiskManager.infer_min_risk_reward_ratio_from_levels(
                        entry_price=float(first_signal["price"]),
                        stop_loss=float(first_signal["stop_loss"]),
                        take_profit=float(first_signal["take_profit"]),
                    )
                rm.evaluate_trade(
                    portfolio_value=max(100.0, float(cfg.capital)),
                    entry_price=float(first_signal["price"]),
                    stop_loss=float(first_signal["stop_loss"]),
                    take_profit=float(first_signal["take_profit"]),
                    strategy_score=float(first_signal["score"]),
                    min_risk_reward_ratio=strategy_rr_min,
                )
                risk_ok = True
            except Exception as exc:
                errors.append(f"risk_error:{exc}")

        paper_ok = False
        if not errors:
            try:
                trader = PaperTrader(strategy=create_strategy(strategy_name), timeframe=cfg.timeframe)
                trader._strategy.initialize()
                trader.run(window.tail(min(len(window), 300)), symbol=cfg.symbol, timeframe=cfg.timeframe)
                paper_ok = True
            except Exception as exc:
                errors.append(f"paper_error:{exc}")

        return {
            "passed": len(errors) == 0,
            "signals_generated": int(signal_count),
            "risk_integration": risk_ok,
            "paper_integration": paper_ok,
            "errors": errors,
        }

    def _run_backtest(self, strategy_name: str, df: pd.DataFrame, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        result = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.capital)).run(
            df,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
        )
        metrics = result.metrics
        return {
            "number_of_trades": int(metrics.total_trades),
            "profit_factor": float(metrics.profit_factor),
            "sharpe": float(metrics.sharpe_ratio),
            "expectancy": float(metrics.expectancy),
            "drawdown_pct": float(metrics.max_drawdown_pct),
            "win_rate": float(metrics.win_rate),
            "net_profit": float(metrics.net_profit),
            "return_pct": float(metrics.return_pct),
        }

    def _run_baseline_backtest(self, df: pd.DataFrame, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        try:
            metrics = self._run_backtest("ClassicEMACrossover", df, cfg)
            return {
                "strategy": "ClassicEMACrossover",
                "status": "ok",
                "metrics": metrics,
            }
        except Exception as exc:
            return {
                "strategy": "ClassicEMACrossover",
                "status": "failed",
                "reason": str(exc),
                "metrics": {},
            }

    def _compare_to_baseline(self, candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        base_metrics = baseline.get("metrics", {}) if isinstance(baseline, dict) else {}
        if not isinstance(base_metrics, dict) or not base_metrics:
            return {
                "status": "no_baseline",
                "classification": "INCONCLUSIVE",
                "decision": "INCONCLUSIVE",
                "reason": str((baseline or {}).get("reason", "baseline_unavailable")) if isinstance(baseline, dict) else "baseline_unavailable",
            }

        candidate_return = float(candidate.get("return_pct", 0.0) or 0.0)
        baseline_return = float(base_metrics.get("return_pct", 0.0) or 0.0)
        delta_return = candidate_return - baseline_return

        if delta_return > 1e-9:
            decision = "SUPEROU"
        elif delta_return < -1e-9:
            decision = "ABAIXO"
        else:
            decision = "EMPATOU"

        return {
            "status": "ok",
            "classification": decision,
            "decision": decision,
            "baseline_strategy": "ClassicEMACrossover",
            "candidate_return_pct": candidate_return,
            "baseline_return_pct": baseline_return,
            "delta_return_pct": delta_return,
        }

    def _early_stop(self, backtest: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        trades = int(backtest.get("number_of_trades", 0))
        pf = float(backtest.get("profit_factor", 0.0))
        expectancy = float(backtest.get("expectancy", 0.0))
        sharpe = float(backtest.get("sharpe", 0.0))
        min_trades = int(settings.validation.min_trades)

        if trades <= 0:
            reasons.append("no_trades")
            return {
                "triggered": True,
                "classification": "NO_TRADES",
                "reasons": reasons,
                "details": {
                    "trades_observed": trades,
                    "min_trades_required": min_trades,
                    "description": "No closed trades were generated in the evaluated window.",
                },
            }

        if trades < min_trades:
            reasons.append("insufficient_sample")
            return {
                "triggered": True,
                "classification": "INCONCLUSIVE_LOW_SAMPLE",
                "reasons": reasons,
                "details": {
                    "trades_observed": trades,
                    "min_trades_required": min_trades,
                    "description": "A estrategia ainda nao possui amostra suficiente para avaliacao estatisticamente robusta.",
                },
            }

        if pf < max(0.30, float(settings.validation.min_profit_factor) * 0.5):
            reasons.append("profit_factor_too_low")
        if expectancy < 0 and sharpe < 0 and pf < 1.0:
            reasons.append("negative_profile")

        return {
            "triggered": len(reasons) > 0,
            "classification": "REJECTED_BY_PERFORMANCE" if len(reasons) > 0 else "continue",
            "reasons": reasons,
            "details": {
                "trades_observed": trades,
                "min_trades_required": min_trades,
            },
        }

    def _run_optimizer(
        self,
        strategy_name: str,
        cfg: Phase13ContinuousFactoryConfig,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        optimizer = StrategyOptimizer(output_dir=self._results_dir)
        execution_id = f"phase13_{self._canon(strategy_name)}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        cpu_budget_workers = max(1, int(max(1.0, float(cfg.max_cpu_per_worker_pct)) // 25))
        effective_workers = min(max(1, int(cfg.optimizer_workers)), cpu_budget_workers)
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                start=start_dt,
                end=end_dt,
                capital=max(100.0, float(cfg.capital)),
                top_n=5,
                workers=effective_workers,
                max_combinations=max(5, int(cfg.optimizer_max_combinations)),
                diagnostic=False,
                execution_id=execution_id,
                strategy_name=strategy_name,
                strategy_version="v1",
                git_commit=os.getenv("GIT_COMMIT"),
                host=socket.gethostname(),
                cpu=platform.processor() or None,
                python_version=platform.python_version(),
            )
        )
        rows = []
        for item in summary.top_results:
            rows.append(
                {
                    "rank": int(item.rank or 0),
                    "parameters": dict(item.parameters),
                    "metrics": dict(item.metrics),
                }
            )
        return {
            "execution_id": execution_id,
            "combinations_tested": int(summary.combinations_tested),
            "duration_seconds": float(summary.duration_seconds),
            "effective_workers": int(effective_workers),
            "top_results": rows,
        }

    def _run_optimizer_probe(
        self,
        strategy_name: str,
        cfg: Phase13ContinuousFactoryConfig,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        optimizer = StrategyOptimizer(output_dir=self._results_dir)
        execution_id = f"phase13_probe_{self._canon(strategy_name)}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        cpu_budget_workers = max(1, int(max(1.0, float(cfg.max_cpu_per_worker_pct)) // 25))
        effective_workers = min(max(1, int(cfg.optimizer_workers)), cpu_budget_workers)
        probe_combinations = max(5, min(10, int(cfg.probe_max_combinations)))
        probe_top_n = max(1, min(3, int(cfg.probe_top_n)))

        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                start=start_dt,
                end=end_dt,
                capital=max(100.0, float(cfg.capital)),
                top_n=probe_top_n,
                workers=effective_workers,
                max_combinations=probe_combinations,
                diagnostic=False,
                execution_id=execution_id,
                strategy_name=strategy_name,
                strategy_version="v1",
                git_commit=os.getenv("GIT_COMMIT"),
                host=socket.gethostname(),
                cpu=platform.processor() or None,
                python_version=platform.python_version(),
            )
        )

        rows: list[dict[str, Any]] = []
        for item in summary.top_results:
            rows.append(
                {
                    "rank": int(item.rank or 0),
                    "parameters": dict(item.parameters),
                    "metrics": dict(item.metrics),
                }
            )

        best = rows[0] if rows else {}
        return {
            "execution_id": execution_id,
            "combinations_tested": int(summary.combinations_tested),
            "duration_seconds": float(summary.duration_seconds),
            "effective_workers": int(effective_workers),
            "probe_budget_combinations": int(probe_combinations),
            "best_rank": int(best.get("rank", 0)) if best else None,
            "best_parameters": dict(best.get("parameters", {})) if best else {},
            "best_metrics_raw": dict(best.get("metrics", {})) if best else {},
            "top_results": rows,
        }

    def _extract_probe_best_metrics(self, probe_report: dict[str, Any]) -> dict[str, Any] | None:
        metrics = probe_report.get("best_metrics_raw", {}) if isinstance(probe_report, dict) else {}
        if not isinstance(metrics, dict) or not metrics:
            return None
        return {
            "number_of_trades": int(metrics.get("total_trades", metrics.get("trades", 0)) or 0),
            "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
            "sharpe": float(metrics.get("sharpe_ratio", metrics.get("sharpe", 0.0)) or 0.0),
            "expectancy": float(metrics.get("expectancy", 0.0) or 0.0),
            "drawdown_pct": float(metrics.get("max_drawdown_pct", metrics.get("drawdown", 0.0)) or 0.0),
            "win_rate": float(metrics.get("win_rate", 0.0) or 0.0),
            "net_profit": float(metrics.get("net_profit", 0.0) or 0.0),
            "return_pct": float(metrics.get("return_pct", metrics.get("return_percent", 0.0)) or 0.0),
        }

    def _metrics_delta_pct(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
        keys = [
            "profit_factor",
            "sharpe",
            "expectancy",
            "win_rate",
            "number_of_trades",
            "drawdown_pct",
            "net_profit",
            "return_pct",
        ]
        out: dict[str, float] = {}
        for key in keys:
            b = float(before.get(key, 0.0) or 0.0)
            a = float(after.get(key, 0.0) or 0.0)
            if abs(b) < 1e-9:
                out[key] = 0.0 if abs(a) < 1e-9 else 100.0
            else:
                out[key] = round(((a - b) / abs(b)) * 100.0, 4)
        return out

    def _run_validation(
        self,
        strategy_name: str,
        cfg: Phase13ContinuousFactoryConfig,
        start_dt: datetime,
        end_dt: datetime,
        optimizer_report: dict[str, Any],
    ) -> dict[str, Any]:
        top_rows = optimizer_report.get("top_results", [])
        if not top_rows:
            return {"approved": False, "reasons": ["no_optimizer_results"]}

        from optimizer.optimization_result import OptimizationResult

        opt_results = [
            OptimizationResult(
                rank=int(item.get("rank", 0)),
                parameters=dict(item.get("parameters", {})),
                metrics=dict(item.get("metrics", {})),
                combinations_tested=int(optimizer_report.get("combinations_tested", 0)),
                runtime_seconds=float(optimizer_report.get("duration_seconds", 0.0)),
                error=None,
            )
            for item in top_rows
        ]

        criteria = ValidationCriteria(
            min_trades=int(settings.validation.min_trades),
            min_profit_factor=float(settings.validation.min_profit_factor),
            max_drawdown_pct=float(settings.validation.max_drawdown_pct),
            min_win_rate_pct=float(settings.validation.min_win_rate_pct),
            min_expectancy=float(settings.validation.min_expectancy),
            min_sharpe=float(settings.validation.min_sharpe),
        )
        validator = OptimizationValidator(criteria=criteria, output_dir=self._results_dir, strategy_name=strategy_name)
        val_window = default_validation_window(start_dt, end_dt, symbol=cfg.symbol, timeframe=cfg.timeframe)

        try:
            summary = validator.validate(
                optimization_results=opt_results,
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                capital=max(100.0, float(cfg.capital)),
                train_start=val_window.train_start,
                train_end=val_window.train_end,
                validation_start=val_window.validation_start,
                validation_end=val_window.validation_end,
                top_n=5,
            )
        except Exception as exc:
            return {"approved": False, "reasons": [f"validation_error:{exc}"]}

        approved = bool(summary.passed > 0 and summary.best_validated is not None)
        snapshot = self._load_validation_snapshot(list(summary.output_files))
        return {
            "approved": approved,
            "total_candidates": int(summary.total_candidates),
            "passed": int(summary.passed),
            "discarded": int(summary.discarded),
            "best_rank": int(summary.best_validated.rank) if summary.best_validated else None,
            "reasons": [] if approved else ["validation_threshold_not_reached"],
            "snapshot": snapshot,
            "output_files": list(summary.output_files),
        }

    def _load_validation_snapshot(self, output_files: list[str]) -> dict[str, Any]:
        csv_candidates = [Path(x) for x in output_files if str(x).lower().endswith(".csv")]
        if not csv_candidates:
            return {}
        csv_path = csv_candidates[0]
        if not csv_path.exists():
            return {}
        try:
            frame = pd.read_csv(csv_path)
            if frame.empty:
                return {}
            row = frame.iloc[0].to_dict()
            return {
                "rank": int(row.get("rank", 0) or 0),
                "passed": bool(row.get("passed", False)),
                "overfitting_risk": bool(row.get("overfitting_risk", False)),
                "discard_reasons": str(row.get("discard_reasons", "")),
                "train_total_trades": int(row.get("train_total_trades", 0) or 0),
                "train_profit_factor": float(row.get("train_profit_factor", 0.0) or 0.0),
                "train_expectancy": float(row.get("train_expectancy", 0.0) or 0.0),
                "train_sharpe_ratio": float(row.get("train_sharpe_ratio", 0.0) or 0.0),
                "validation_total_trades": int(row.get("validation_total_trades", 0) or 0),
                "validation_profit_factor": float(row.get("validation_profit_factor", 0.0) or 0.0),
                "validation_expectancy": float(row.get("validation_expectancy", 0.0) or 0.0),
                "validation_sharpe_ratio": float(row.get("validation_sharpe_ratio", 0.0) or 0.0),
            }
        except Exception:
            return {}

    def _prepare_paper_trading(self, strategy_name: str, df: pd.DataFrame, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        try:
            trader = PaperTrader(strategy=create_strategy(strategy_name), timeframe=cfg.timeframe)
            trader._strategy.initialize()
            summary = trader.run(df.tail(min(len(df), 600)), symbol=cfg.symbol, timeframe=cfg.timeframe)
            return {
                "status": "running",
                "closed_trades": int(summary.get("closed_trades", 0)),
                "net_profit": float(summary.get("net_profit", 0.0)),
                "message": "Paper trading qualification started without replacing active strategies.",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "reason": str(exc),
            }

    def _is_superior_to_approved(self, candidate: dict[str, Any], backlog: list[dict[str, Any]]) -> dict[str, Any]:
        current_pf = float((candidate.get("final_metrics") or {}).get("profit_factor", 0.0))
        approved = [
            x
            for x in backlog
            if x.get("candidate_name") != candidate.get("candidate_name")
            and x.get("state") in {"approved", "in_paper_trading"}
        ]
        if not approved:
            return {"is_superior": True, "reference": None, "decision": "SIM"}

        best = max(approved, key=lambda x: float((x.get("final_metrics") or {}).get("profit_factor", 0.0)))
        best_pf = float((best.get("final_metrics") or {}).get("profit_factor", 0.0))
        return {
            "is_superior": current_pf > best_pf,
            "reference": best.get("candidate_name"),
            "decision": "SIM" if current_pf > best_pf else "NAO",
        }

    def _build_report(
        self,
        run_id: str,
        started_at: datetime,
        cfg: Phase13ContinuousFactoryConfig,
        backlog: list[dict[str, Any]],
        processed: list[dict[str, Any]],
        stop_reason: str,
        campaign_seconds: float,
        stage_counters: dict[str, int],
        coverage_before: dict[str, Any],
        coverage_after: dict[str, Any],
        rejection_knowledge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        implemented = [x for x in backlog if str(x.get("state", "")) not in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}]
        rejected_performance = [x for x in backlog if x.get("state") in {"REJECTED_BY_PERFORMANCE", "REJECTED", "REJECTED_AFTER_PAPER_EXPERIMENT"}]
        rejected_infra = [x for x in backlog if x.get("state") == "REJECTED_BY_INFRASTRUCTURE"]
        no_trades = [x for x in backlog if x.get("state") == "NO_TRADES"]
        low_sample = [x for x in backlog if x.get("state") == "INCONCLUSIVE_LOW_SAMPLE"]
        rejected = rejected_performance + rejected_infra
        approved = [x for x in backlog if x.get("state") in {"approved", "PAPER_APPROVED"}]
        in_paper = [x for x in backlog if x.get("state") in {"in_paper_trading", "PAPER_APPROVED"}]
        paper_candidates = [x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]
        # paper_candidates classified in THIS run specifically (not cumulative backlog)
        paper_candidates_this_run = [x for x in processed if x.get("state") == "PAPER_CANDIDATE"]
        paper_experimental_started = [
            x for x in backlog if isinstance(x.get("paper_experimental"), dict)
        ]
        paper_experimental_running = [
            x
            for x in paper_experimental_started
            if str((x.get("paper_experimental") or {}).get("status", "")).lower() == "running"
        ]
        pending = [
            x
            for x in backlog
            if self._is_queue_eligible(x)
            and str(x.get("last_processed_run_id", "")) != str(run_id)
        ]
        inconclusive = [
            x
            for x in backlog
            if x.get("state") in {"INCONCLUSIVE_RESOURCE_LIMIT", "INCONCLUSIVE", "INCONCLUSIVE_LOW_SAMPLE", "NO_TRADES"}
        ]

        durations = [float(x.get("processing_seconds", 0.0)) for x in processed]
        avg_time = sum(durations) / len(durations) if durations else 0.0

        rejection_causes: dict[str, int] = {}
        for row in rejected:
            reason = str(row.get("rejection_reason", "unknown"))
            rejection_causes[reason] = rejection_causes.get(reason, 0) + 1

        ranking_updated = []
        for idx, item in enumerate(sorted(backlog, key=lambda x: float(x.get("queue_score", 0.0)), reverse=True), start=1):
            ranking_updated.append(
                {
                    "rank": idx,
                    "candidate_name": item.get("candidate_name"),
                    "state": item.get("state"),
                    "queue_score": round(float(item.get("queue_score", 0.0)), 4),
                    "priority_penalty": round(float(item.get("priority_penalty", 0.0)), 4),
                }
            )

        consolidated = self._consolidated_learning(backlog)

        comparisons = [x.get("pipeline_comparison", {}) for x in processed if isinstance(x.get("pipeline_comparison"), dict)]
        changed_decisions = 0
        reduced_false_negatives = 0
        probe_gains_pf: list[float] = []
        probe_gains_sharpe: list[float] = []
        continued_eliminated = 0
        for comp in comparisons:
            old_elim = str((comp.get("old_pipeline") or {}).get("eliminated", "SIM"))
            new_elim = str((comp.get("new_pipeline") or {}).get("eliminated", "SIM"))
            if old_elim != new_elim:
                changed_decisions += 1
            if old_elim == "SIM" and new_elim == "NAO":
                reduced_false_negatives += 1
            if new_elim == "SIM":
                continued_eliminated += 1

        for row in processed:
            reassess = row.get("probe_reassessment") if isinstance(row.get("probe_reassessment"), dict) else {}
            deltas = reassess.get("metric_deltas_pct") if isinstance(reassess.get("metric_deltas_pct"), dict) else {}
            if "profit_factor" in deltas:
                probe_gains_pf.append(float(deltas.get("profit_factor", 0.0)))
            if "sharpe" in deltas:
                probe_gains_sharpe.append(float(deltas.get("sharpe", 0.0)))

        avg_gain_pf = round(sum(probe_gains_pf) / len(probe_gains_pf), 4) if probe_gains_pf else 0.0
        avg_gain_sharpe = round(sum(probe_gains_sharpe) / len(probe_gains_sharpe), 4) if probe_gains_sharpe else 0.0
        comparison_rows: list[dict[str, Any]] = []
        for row in processed:
            comp = row.get("pipeline_comparison") if isinstance(row.get("pipeline_comparison"), dict) else {}
            oldp = comp.get("old_pipeline") if isinstance(comp.get("old_pipeline"), dict) else {}
            newp = comp.get("new_pipeline") if isinstance(comp.get("new_pipeline"), dict) else {}
            comparison_rows.append(
                {
                    "candidate_name": row.get("candidate_name"),
                    "old_eliminated": oldp.get("eliminated", "SIM"),
                    "new_eliminated": newp.get("eliminated", "SIM"),
                    "old_metrics": oldp.get("metrics", {}),
                    "new_metrics": newp.get("metrics", {}),
                    "old_reasons": oldp.get("early_stop_reasons", []),
                    "new_reasons": newp.get("early_stop_reasons", []),
                }
            )

        baseline_rows = [
            x.get("baseline_comparison", {})
            for x in processed
            if isinstance(x.get("baseline_comparison"), dict)
        ]
        baseline_superou = len([x for x in baseline_rows if str(x.get("decision", "")).upper() == "SUPEROU"])
        baseline_empatou = len([x for x in baseline_rows if str(x.get("decision", "")).upper() == "EMPATOU"])
        baseline_abaixo = len([x for x in baseline_rows if str(x.get("decision", "")).upper() == "ABAIXO"])
        baseline_inconclusive = len(baseline_rows) - baseline_superou - baseline_empatou - baseline_abaixo

        return {
            "phase": "13",
            "pipeline_version": "13.6_paper_candidate",
            "run_id": run_id,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "started_at": started_at.isoformat(),
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "target_approved": int(cfg.target_approved),
            "stop_reason": stop_reason,
            "implemented_count": len(implemented),
            "rejected_count": len(rejected),
            "rejected_performance_count": len(rejected_performance),
            "rejected_infrastructure_count": len(rejected_infra),
            "no_trades_count": len(no_trades),
            "inconclusive_low_sample_count": len(low_sample),
            "inconclusive_count": len(inconclusive),
            "pending_count": len(pending),
            "approved_count": len(approved),
            "in_paper_trading_count": len(in_paper),
            "paper_candidate_count": len(paper_candidates),
            "paper_experimental_started_count": len(paper_experimental_started),
            "paper_experimental_running_count": len(paper_experimental_running),
            "average_time_per_strategy_seconds": round(avg_time, 3),
            "total_campaign_seconds": float(campaign_seconds),
            "top_rejection_reasons": sorted(rejection_causes.items(), key=lambda x: x[1], reverse=True),
            "stage_counters": stage_counters,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "ranking_updated": ranking_updated,
            "backlog": backlog,
            "knowledge_base": self._load_state().get("rejection_knowledge", []),
            "continuous_learning_analysis": consolidated,
            "pipeline_old_vs_new": comparison_rows,
            "baseline_official": {
                "strategy": "ClassicEMACrossover",
                "comparisons_total": len(baseline_rows),
                "superou": baseline_superou,
                "empatou": baseline_empatou,
                "abaixo": baseline_abaixo,
                "inconclusive": baseline_inconclusive,
            },
            "answers": {
                "strategies_researched": coverage_after.get("strategies_researched", 0),
                "implemented_in_campaign": int(stage_counters.get("implementation_real", 0)),
                "effectively_evaluated": int(
                    stage_counters.get("backtest_reached", 0)
                ),
                "reprocessed_strategies": int(stage_counters.get("reprocessed_total", len(processed))),
                "passed_optimizer_probe": int(stage_counters.get("optimizer_probe_reached", 0)),
                "continued_eliminated_after_probe": int(continued_eliminated),
                "reached_full_optimizer": int(stage_counters.get("optimizer_reached", 0)),
                "reached_validation": int(stage_counters.get("validation_reached", 0)),
                "reached_paper_qualification": int(stage_counters.get("paper_qualification_reached", 0)),
                "reached_paper_trading": int(len(in_paper)),
                "classified_paper_candidate": int(len(paper_candidates_this_run)),
                "classified_paper_candidate_cumulative": int(len(paper_candidates)),
                "paper_experimental_started": int(len(paper_experimental_started)),
                "paper_experimental_running": int(len(paper_experimental_running)),
                "avg_probe_gain_profit_factor_pct": avg_gain_pf,
                "avg_probe_gain_sharpe_pct": avg_gain_sharpe,
                "changed_decision_vs_old_pipeline": int(changed_decisions),
                "rejected_performance": len(rejected_performance),
                "rejected_infrastructure": len(rejected_infra),
                "no_trades": len(no_trades),
                "inconclusive_low_sample": len(low_sample),
                "approved": len(approved),
                "in_paper_trading": len(in_paper),
                "pending": len(pending),
                "coverage_before_pct": coverage_before.get("coverage_pct", 0.0),
                "coverage_after_pct": coverage_after.get("coverage_pct", 0.0),
                "avg_time_per_strategy_seconds": round(avg_time, 3),
                "total_campaign_seconds": float(campaign_seconds),
                "optimizer_probe_reduced_false_negatives": "SIM" if reduced_false_negatives > 0 else "NAO",
                "optimizer_probe_altered_scientific_criteria": "NAO",
                "optimizer_probe_only_minimal_param_opportunity": "SIM",
                "paper_candidate_enabled": "SIM",
                "baseline_strategy": "ClassicEMACrossover",
                "baseline_superou": baseline_superou,
                "baseline_empatou": baseline_empatou,
                "baseline_abaixo": baseline_abaixo,
                "baseline_inconclusive": baseline_inconclusive,
            },
            "answer": {
                "question": "Quantas estrategias aptas para Paper Trading foram encontradas?",
                "value": len(in_paper),
            },
        }

    def _record_rejection_knowledge(self, item: dict[str, Any], rejection_knowledge: dict[str, Any]) -> None:
        """Record rejection data for learning and future prioritization."""
        family = item.get("family", "unknown")
        indicators = item.get("indicators", [])
        stage = item.get("rejection_stage", "unknown")
        metrics = item.get("final_metrics", {})
        
        # Track by family
        if family not in rejection_knowledge["family"]:
            rejection_knowledge["family"][family] = {"count": 0, "reasons": []}
        rejection_knowledge["family"][family]["count"] += 1
        rejection_knowledge["family"][family]["reasons"].append(item.get("rejection_reason", "unknown"))
        
        # Track by indicators
        for ind in indicators:
            if ind not in rejection_knowledge["indicators"]:
                rejection_knowledge["indicators"][ind] = {"count": 0, "stage": stage}
            rejection_knowledge["indicators"][ind]["count"] += 1
        
        # Track by stage
        if stage not in rejection_knowledge["stage"]:
            rejection_knowledge["stage"][stage] = 0
        rejection_knowledge["stage"][stage] += 1
        
        # Add complete record
        rejection_knowledge["all_rejections"].append({
            "candidate_name": item.get("candidate_name"),
            "family": family,
            "indicators": indicators,
            "stage": stage,
            "reason": item.get("rejection_reason"),
            "metrics": metrics,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })

    def _consolidated_learning(self, backlog: list[dict[str, Any]]) -> dict[str, Any]:
        approved = [x for x in backlog if x.get("state") in {"approved", "in_paper_trading", "PAPER_APPROVED", "PAPER_CANDIDATE"}]
        rejected = [x for x in backlog if x.get("state") in {"REJECTED_BY_PERFORMANCE", "REJECTED_BY_INFRASTRUCTURE", "REJECTED", "REJECTED_AFTER_PAPER_EXPERIMENT"}]

        fam_totals: dict[str, int] = {}
        fam_approved: dict[str, int] = {}
        for row in backlog:
            fam = str(row.get("family", "indefinida"))
            fam_totals[fam] = fam_totals.get(fam, 0) + 1
        for row in approved:
            fam = str(row.get("family", "indefinida"))
            fam_approved[fam] = fam_approved.get(fam, 0) + 1
        family_rates = []
        for fam, total in fam_totals.items():
            ap = fam_approved.get(fam, 0)
            family_rates.append(
                {
                    "family": fam,
                    "approved": ap,
                    "tested": total,
                    "approval_rate": round((ap / total) if total > 0 else 0.0, 4),
                }
            )
        family_rates.sort(key=lambda x: x["approval_rate"], reverse=True)

        indicators_ok = self._indicator_counts(approved)
        indicators_bad = self._indicator_counts(rejected)

        combo_scores: dict[str, int] = {}
        for row in approved:
            inds = sorted([self._canon(x) for x in row.get("indicators", []) if str(x).strip()])
            if not inds:
                continue
            key = "+".join(inds)
            combo_scores[key] = combo_scores.get(key, 0) + 1

        assets = self._value_counts(backlog, "assets")
        timeframes = self._value_counts(backlog, "timeframes")

        recurring_patterns = []
        for fam, total in sorted(fam_totals.items(), key=lambda x: x[1], reverse=True):
            recurring_patterns.append(
                {
                    "pattern": f"family={fam}",
                    "frequency": total,
                    "approved": fam_approved.get(fam, 0),
                }
            )

        return {
            "family_approval_rates": family_rates,
            "indicators_in_approved": indicators_ok[:10],
            "indicators_in_rejected": indicators_bad[:10],
            "indicator_combinations_with_potential": [
                {"combination": k, "approved_count": v}
                for k, v in sorted(combo_scores.items(), key=lambda x: x[1], reverse=True)[:10]
            ],
            "best_assets": assets[:10],
            "best_timeframes": timeframes[:10],
            "recurring_patterns": recurring_patterns[:10],
        }

    def _indicator_counts(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            for indicator in row.get("indicators", []):
                name = str(indicator).strip()
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + 1
        return [{"indicator": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)]

    def _value_counts(self, rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            values = row.get(field, [])
            if isinstance(values, str):
                values = [values]
            if not values and field == "assets":
                values = ["BTC/USDT"]
            for value in values:
                name = str(value).strip()
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + 1
        return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)]

    def _reject_item(self, item: dict[str, Any], stage: str, reason: str) -> None:
        item["state"] = "rejected"
        item["rejection_stage"] = stage
        item["rejection_reason"] = reason
        item["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        item.setdefault("history", []).append(
            {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "event": "rejected",
                "stage": stage,
                "reason": reason,
            }
        )

    def _priority_penalty(self, item: dict[str, Any], state: dict[str, Any]) -> float:
        knowledge = state.get("rejection_knowledge", [])
        family = self._canon(str(item.get("family", "")))
        indicators = {self._canon(x) for x in item.get("indicators", [])}

        penalty = 0.0
        repeated_family = 0
        for row in knowledge:
            row_family = self._canon(str(row.get("family", "")))
            row_indicators = {self._canon(x) for x in row.get("indicators", [])}
            if row_family == family and family:
                repeated_family += 1
            if indicators and row_indicators:
                inter = len(indicators.intersection(row_indicators))
                union = len(indicators.union(row_indicators))
                sim = (inter / union) if union > 0 else 0.0
                if sim >= 0.75:
                    penalty += 8.0
                elif sim >= 0.50:
                    penalty += 4.0

        if repeated_family >= 3:
            penalty += 10.0
        elif repeated_family >= 2:
            penalty += 5.0
        return penalty

    def _register_rejection_knowledge(self, item: dict[str, Any], stage: str, metrics: dict[str, Any]) -> None:
        state = self._load_state()
        kb = state.setdefault("rejection_knowledge", [])
        kb.append(
            {
                "candidate_name": item.get("candidate_name"),
                "family": item.get("family"),
                "indicators": item.get("indicators", []),
                "assets": [item.get("symbol", "BTC/USDT")],
                "timeframes": item.get("timeframes", []),
                "stage": stage,
                "reason": item.get("rejection_reason") or "unknown",
                "metrics": metrics,
                "parameters": (item.get("validation") or {}).get("best_rank"),
                "ts": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
        state["rejection_knowledge"] = kb[-500:]
        self._save_state(state.get("backlog", []), state)

    def _approved_count(self, backlog: list[dict[str, Any]]) -> int:
        return len([x for x in backlog if x.get("state") in {"approved", "in_paper_trading", "PAPER_APPROVED"}])

    def _coverage_snapshot(self, backlog: list[dict[str, Any]]) -> dict[str, Any]:
        researched = len(backlog)
        implemented = len([x for x in backlog if str(x.get("state", "")) not in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}])
        pending_states = {
            "researched",
            "queued",
            "awaiting_evaluation",
            "implemented",
            "IMPLEMENTATION_PENDING",
            "IMPLEMENTATION_INCOMPLETE",
            "INCONCLUSIVE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
            "INCONCLUSIVE_LOW_SAMPLE",
            "NO_TRADES",
        }
        pending = len([x for x in backlog if str(x.get("state", "")) in pending_states])
        evaluated = len(
            [
                x
                for x in backlog
                if x.get("state")
                in {
                    "REJECTED_BY_PERFORMANCE",
                    "REJECTED_BY_INFRASTRUCTURE",
                    "REJECTED",
                    "REJECTED_AFTER_PAPER_EXPERIMENT",
                    "approved",
                    "in_paper_trading",
                    "PAPER_APPROVED",
                    "PAPER_CANDIDATE",
                    "INCONCLUSIVE_RESOURCE_LIMIT",
                    "INCONCLUSIVE_LOW_SAMPLE",
                    "NO_TRADES",
                }
            ]
        )
        coverage_pct = round((float(implemented) / float(max(1, researched))) * 100.0, 2)
        return {
            "strategies_researched": researched,
            "strategies_implemented": implemented,
            "strategies_effectively_evaluated": evaluated,
            "strategies_pending": pending,
            "coverage_pct": coverage_pct,
        }

    def _family_from_row(self, row: dict[str, Any]) -> str:
        raw = str(row.get("type") or row.get("category") or row.get("family") or "").strip().lower()
        mapping = {
            "trend following": "tendencia",
            "trend": "tendencia",
            "tendencia": "tendencia",
            "momentum": "momentum",
            "breakout": "breakout",
            "reversao": "reversao",
            "mean reversion": "reversao",
            "volatility": "volatilidade",
            "volatilidade": "volatilidade",
            "hybrid": "hibridas",
            "hibrida": "hibridas",
            "hibridas": "hibridas",
        }
        return mapping.get(raw, raw or "indefinida")

    def _coerce_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return []
            if cleaned.startswith("[") and cleaned.endswith("]"):
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    pass
            return [x.strip() for x in cleaned.split(",") if x.strip()]
        return []

    def _load_phase14_seed_backlog(self) -> list[dict[str, Any]]:
        path = self._results_dir / "phase14_seed_backlog.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("seed_backlog", []) if isinstance(raw, dict) else []
            return [dict(item) for item in rows if isinstance(item, dict)]
        except Exception:
            return []

    def _priority_label_from_classification(self, value: str) -> str:
        raw = self._canon(value)
        if "alta" in raw:
            return "Alta"
        if "media" in raw:
            return "Media"
        if "baixa" in raw:
            return "Baixa"
        return "Baixa"

    def _canon(self, value: str) -> str:
        return value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")

    def _state_path(self) -> Path:
        return self._results_dir / "phase13_factory_state.json"

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {"backlog": [], "rejection_knowledge": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("backlog", [])
                raw.setdefault("rejection_knowledge", [])
                return raw
        except Exception:
            logger.warning("Failed to load phase13 state, rebuilding.")
        return {"backlog": [], "rejection_knowledge": []}

    def _save_state(self, backlog: list[dict[str, Any]], state: dict[str, Any] | None = None) -> None:
        payload = state or self._load_state()
        payload["backlog"] = backlog
        self._state_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}_backlog.csv"
        rows = report.get("backlog", [])
        if rows:
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "candidate_name",
                        "platform_strategy_name",
                        "family",
                        "state",
                        "queue_score",
                        "priority_penalty",
                        "rejection_stage",
                        "rejection_reason",
                    ],
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(rows)
            outputs["csv"] = str(csv_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _monitor_dir(self) -> Path:
        return self._base_dir / "optimization" / "monitor"

    def _monitor_readme_text(self) -> str:
        return "\n".join(
            [
                "Optimization Monitor API",
                "========================",
                "",
                "Fixed files written by the overnight campaign:",
                "- current_status.json",
                "- current_progress.json",
                "- current_performance.json",
                "- current_last_event.txt",
                "- current_summary.txt",
                "- campaign_finished.flag",
                "",
                "Rules:",
                "- Read these files directly; do not search for timestamped artifacts.",
                "- phase13_factory_state.json remains the campaign source of truth.",
                "- campaign_finished.flag exists only after a completed campaign run.",
                "- current_status.json is the official monitoring source.",
                "- current_status.json mandatory keys:",
                "  campaign_id,status,stop_reason,total_strategies,processed_strategies,pending_strategies,completion_pct,",
                "  paper_candidates,paper_approved,paper_experimental,inconclusive_low_sample,rejected_by_performance,",
                "  rejected_by_infrastructure,rejected_total,current_stage,current_strategy,heartbeat,last_update,state_revision,",
                "  api_version,schema_version",
                "- consistency validation before save: processed_strategies + pending_strategies == total_strategies",
                "- if consistency fails, current_status.json must not be updated and an error is logged.",
                "",
            ]
        )

    def _write_monitor_api(
        self,
        run_id: str,
        payload: dict[str, Any],
        cfg: Phase13ContinuousFactoryConfig | None = None,
        report: dict[str, Any] | None = None,
        final: bool = False,
    ) -> None:
        monitor_dir = self._monitor_dir()
        monitor_dir.mkdir(parents=True, exist_ok=True)

        base_report = report or payload
        status = str(payload.get("status") or base_report.get("stop_reason") or "running")
        generated_at = datetime.now(tz=timezone.utc).isoformat()
        pending_states = {
            "researched",
            "queued",
            "awaiting_evaluation",
            "implemented",
            "IMPLEMENTATION_PENDING",
            "IMPLEMENTATION_INCOMPLETE",
            "INCONCLUSIVE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
            "INCONCLUSIVE_LOW_SAMPLE",
            "NO_TRADES",
            "PAPER_CANDIDATE",
        }

        backlog_rows = base_report.get("backlog", [])
        if not isinstance(backlog_rows, list) or not backlog_rows:
            state = self._load_state()
            state_backlog = state.get("backlog", []) if isinstance(state, dict) else []
            backlog_rows = state_backlog if isinstance(state_backlog, list) else []

        total_backlog = len(backlog_rows)
        pending_from_backlog = len([x for x in backlog_rows if str(x.get("state", "")) in pending_states])
        paper_candidates_from_backlog = len([x for x in backlog_rows if str(x.get("state", "")) == "PAPER_CANDIDATE"])
        paper_approved_from_backlog = len([x for x in backlog_rows if str(x.get("state", "")) in {"in_paper_trading", "PAPER_APPROVED"}])
        paper_experimental_from_backlog = len([x for x in backlog_rows if isinstance(x.get("paper_experimental"), dict)])
        inconclusive_low_sample_from_backlog = len([x for x in backlog_rows if str(x.get("state", "")) == "INCONCLUSIVE_LOW_SAMPLE"])
        rejected_by_performance_from_backlog = len(
            [x for x in backlog_rows if str(x.get("state", "")) in {"REJECTED_BY_PERFORMANCE", "REJECTED", "REJECTED_AFTER_PAPER_EXPERIMENT"}]
        )
        rejected_by_infrastructure_from_backlog = len([x for x in backlog_rows if str(x.get("state", "")) == "REJECTED_BY_INFRASTRUCTURE"])
        rejected_total_from_backlog = rejected_by_performance_from_backlog + rejected_by_infrastructure_from_backlog

        total_strategies = int(
            payload.get(
                "total_strategies",
                base_report.get("total_strategies", total_backlog if total_backlog > 0 else 0),
            )
        )
        pending = int(
            payload.get(
                "pending",
                base_report.get("pending_count", pending_from_backlog if total_backlog > 0 else 0),
            )
        )
        if total_backlog > 0 and total_strategies <= 0:
            total_strategies = total_backlog

        processed = int(payload.get("processed", base_report.get("implemented_count", max(total_strategies - pending, 0))))
        if processed < 0:
            processed = 0

        approved = int(payload.get("approved", base_report.get("approved_count", 0)))
        paper_candidates = int(payload.get("paper_candidates", base_report.get("paper_candidate_count", paper_candidates_from_backlog)))
        paper_approved = int(payload.get("paper_approved", base_report.get("in_paper_trading_count", paper_approved_from_backlog)))
        paper_experimental = int(payload.get("paper_experimental", base_report.get("paper_experimental_started_count", paper_experimental_from_backlog)))
        inconclusive_low_sample = int(
            payload.get("inconclusive_low_sample", base_report.get("inconclusive_low_sample_count", inconclusive_low_sample_from_backlog))
        )
        rejected_by_performance = int(
            payload.get("rejected_by_performance", base_report.get("rejected_performance_count", rejected_by_performance_from_backlog))
        )
        rejected_by_infrastructure = int(
            payload.get("rejected_by_infrastructure", base_report.get("rejected_infrastructure_count", rejected_by_infrastructure_from_backlog))
        )
        rejected_total = int(payload.get("rejected", base_report.get("rejected_count", rejected_total_from_backlog)))
        total_processed = int(base_report.get("answers", {}).get("reprocessed_strategies", processed))
        total_seconds = float(base_report.get("total_campaign_seconds", payload.get("campaign_seconds", 0.0)))
        avg_seconds = float(base_report.get("average_time_per_strategy_seconds", 0.0))
        strategies_per_minute = (processed / total_seconds * 60.0) if total_seconds > 0 else 0.0
        completion_pct = (processed / total_strategies * 100.0) if total_strategies > 0 else 0.0
        checkpoint_interval = int(cfg.checkpoint_interval_seconds) if cfg else int(payload.get("checkpoint_interval_seconds", 600))
        optimizer_workers = int(cfg.optimizer_workers) if cfg else int(payload.get("optimizer_workers", 1))
        max_cpu_per_worker_pct = float(cfg.max_cpu_per_worker_pct) if cfg else float(payload.get("max_cpu_per_worker_pct", 100.0))

        stage_map = {
            "selected": "QUEUE",
            "implementation": "IMPLEMENTATION",
            "smoke": "SMOKE",
            "backtest": "BACKTEST",
            "optimizerprobe": "OPTIMIZER_PROBE",
            "optimizer_probe": "OPTIMIZER_PROBE",
            "optimizer": "OPTIMIZER",
            "validation": "VALIDATION",
            "paperqualification": "PAPER_QUALIFICATION",
            "paper_qualification": "PAPER_QUALIFICATION",
            "paperexperimental": "PAPER_EXPERIMENTAL",
            "paper_experimental": "PAPER_EXPERIMENTAL",
            "papertrading": "PAPER_TRADING",
            "paper_trading": "PAPER_TRADING",
            "earlystop": "EARLY_STOP",
            "early_stop": "EARLY_STOP",
            "error": "ERROR",
        }
        latest_strategy_name: str | None = None
        latest_stage_token: str | None = None
        latest_event_ts = ""
        for row in backlog_rows:
            if not isinstance(row, dict):
                continue
            history = row.get("history")
            if not isinstance(history, list) or not history:
                continue
            event_row = history[-1] if isinstance(history[-1], dict) else {}
            event_name = str(event_row.get("event", ""))
            event_stage = event_name.split(":", 1)[0].strip().lower() if event_name else ""
            ts = str(event_row.get("ts", "") or row.get("updated_at", ""))
            if ts >= latest_event_ts:
                latest_event_ts = ts
                latest_strategy_name = str(row.get("candidate_name", "") or "") or None
                latest_stage_token = event_stage or None

        raw_stage = payload.get("current_stage") or payload.get("stage") or latest_stage_token or payload.get("phase") or base_report.get("phase", "13")
        canon_stage = str(raw_stage).strip().lower().replace("-", "_").replace(" ", "_")
        if canon_stage.isdigit():
            current_stage = "CAMPAIGN"
        else:
            current_stage = stage_map.get(canon_stage, str(raw_stage).upper())

        raw_current_strategy = payload.get("current_strategy", payload.get("interrupted_strategy"))
        if raw_current_strategy in ("", None):
            current_strategy = latest_strategy_name
        else:
            current_strategy = str(raw_current_strategy)
        heartbeat = str(payload.get("heartbeat", generated_at))
        last_update = str(payload.get("last_update", generated_at))

        status_path = monitor_dir / "current_status.json"
        previous_state_revision = 0
        if status_path.exists():
            try:
                previous_status = json.loads(status_path.read_text(encoding="utf-8"))
                previous_state_revision = int(previous_status.get("state_revision", 0))
            except Exception:
                previous_state_revision = 0
        state_revision = previous_state_revision + 1

        status_payload = {
            "campaign_id": run_id,
            "status": status,
            "stop_reason": base_report.get("stop_reason", payload.get("stop_reason", status)),
            "total_strategies": total_strategies,
            "processed_strategies": processed,
            "pending_strategies": pending,
            "completion_pct": round(completion_pct, 2),
            "paper_candidates": paper_candidates,
            "paper_approved": paper_approved,
            "paper_experimental": paper_experimental,
            "inconclusive_low_sample": inconclusive_low_sample,
            "rejected_by_performance": rejected_by_performance,
            "rejected_by_infrastructure": rejected_by_infrastructure,
            "rejected_total": rejected_total,
            "current_stage": current_stage,
            "current_strategy": current_strategy,
            "heartbeat": heartbeat,
            "last_update": last_update,
            "state_revision": state_revision,
            "api_version": "1.0",
            "schema_version": "1.0",
        }

        progress_payload = {
            "run_id": run_id,
            "generated_at": generated_at,
            "processed": processed,
            "approved": approved,
            "paper_candidates": paper_candidates,
            "pending": pending,
            "backlog_total": total_strategies,
            "completion_pct": round(completion_pct, 2),
            "stage_counters": base_report.get("stage_counters", {}),
            "answers": base_report.get("answers", {}),
            "checkpoint_interval_seconds": checkpoint_interval,
            "final": bool(final),
        }

        performance_payload = {
            "run_id": run_id,
            "generated_at": generated_at,
            "total_campaign_seconds": round(total_seconds, 3),
            "average_time_per_strategy_seconds": round(avg_seconds, 3),
            "strategies_per_minute": round(strategies_per_minute, 3),
            "processed": processed,
            "total_processed": total_processed,
            "checkpoint_interval_seconds": checkpoint_interval,
            "optimizer_workers": optimizer_workers,
            "max_cpu_per_worker_pct": round(max_cpu_per_worker_pct, 2),
            "pending": pending,
            "approved": approved,
            "paper_candidates": paper_candidates,
            "final": bool(final),
        }

        consistency_ok = (processed + pending) == total_strategies
        if not consistency_ok:
            consistency_msg = (
                "monitor_consistency_error "
                f"campaign_id={run_id} processed={processed} pending={pending} total={total_strategies}"
            )
            logger.error(consistency_msg)
            (monitor_dir / "current_last_event.txt").write_text(f"consistency_error\n{consistency_msg}\n", encoding="utf-8")
            with (monitor_dir / "consistency_errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{generated_at} {consistency_msg}\n")
            return

        stop_reason_value = str(base_report.get("stop_reason", payload.get("stop_reason", status)))
        if stop_reason_value == "no_more_pending_strategies" and pending > 0:
            consistency_msg = (
                "monitor_consistency_error "
                f"campaign_id={run_id} stop_reason=no_more_pending_strategies pending={pending}"
            )
            logger.error(consistency_msg)
            (monitor_dir / "current_last_event.txt").write_text(f"consistency_error\n{consistency_msg}\n", encoding="utf-8")
            with (monitor_dir / "consistency_errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{generated_at} {consistency_msg}\n")
            return

        last_event = (
            "campaign_completed" if final and status != "interrupted" else
            "campaign_interrupted" if final and status == "interrupted" else
            "checkpoint_saved"
        )
        event_details = f"processed={processed} approved={approved} pending={pending} paper_candidates={paper_candidates}"

        (monitor_dir / "README_MONITOR.txt").write_text(self._monitor_readme_text(), encoding="utf-8")
        status_path.write_text(
            json.dumps(status_payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        (monitor_dir / "current_progress.json").write_text(
            json.dumps(progress_payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        (monitor_dir / "current_performance.json").write_text(
            json.dumps(performance_payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        (monitor_dir / "current_last_event.txt").write_text(f"{last_event}\n{event_details}\n", encoding="utf-8")
        summary_lines = [
            f"run_id={run_id}",
            f"status={status}",
            f"stop_reason={base_report.get('stop_reason', payload.get('stop_reason', status))}",
            f"processed={processed}",
            f"approved={approved}",
            f"paper_candidates={paper_candidates}",
            f"pending={pending}",
            f"completion_pct={round(completion_pct, 2)}", 
            f"strategies_per_minute={round(strategies_per_minute, 3)}",
        ]
        (monitor_dir / "current_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

        flag_path = monitor_dir / "campaign_finished.flag"
        if final and status != "interrupted":
            flag_path.write_text(generated_at + "\n", encoding="utf-8")
        elif flag_path.exists():
            flag_path.unlink()

    def _to_markdown(self, report: dict[str, Any]) -> str:
        answers = report.get("answers", {})
        coverage_before = report.get("coverage_before", {})
        coverage_after = report.get("coverage_after", {})
        stages = report.get("stage_counters", {})
        lines = [
            "# FASE 13 - Continuous Strategy Factory",
            "",
            f"- run_id: {report.get('run_id')}",
            f"- pipeline_version: {report.get('pipeline_version', '13')}",
            f"- stop_reason: {report.get('stop_reason')}",
            f"- aprovadas em paper: {report.get('in_paper_trading_count', 0)}",
            f"- paper experimental iniciados: {report.get('paper_experimental_started_count', 0)}",
            f"- reprovadas por desempenho: {report.get('rejected_performance_count', 0)}",
            f"- reprovadas por infraestrutura: {report.get('rejected_infrastructure_count', 0)}",
            f"- tempo medio por estrategia (s): {report.get('average_time_per_strategy_seconds', 0.0)}",
            f"- tempo total da campanha (s): {report.get('total_campaign_seconds', 0.0)}",
            "",
            "## Consolidado 13.6",
            "",
            f"- Quantas estrategias foram reprocessadas? {answers.get('reprocessed_strategies', 0)}",
            f"- Quantas passaram pelo Optimizer Probe? {answers.get('passed_optimizer_probe', 0)}",
            f"- Quantas continuaram eliminadas? {answers.get('continued_eliminated_after_probe', 0)}",
            f"- Quantas chegaram ao Optimizer completo? {answers.get('reached_full_optimizer', 0)}",
            f"- Quantas chegaram ao Validation? {answers.get('reached_validation', 0)}",
            f"- Quantas chegaram ao Paper Qualification? {answers.get('reached_paper_qualification', 0)}",
            f"- Quantas chegaram ao Paper Trading? {answers.get('reached_paper_trading', 0)}",
            f"- Quantas foram classificadas como PAPER_CANDIDATE? {answers.get('classified_paper_candidate', 0)}",
            f"- Quantas iniciaram Paper Experimental? {answers.get('paper_experimental_started', 0)}",
            f"- Ganho medio PF apos Probe (%): {answers.get('avg_probe_gain_profit_factor_pct', 0.0)}",
            f"- Ganho medio Sharpe apos Probe (%): {answers.get('avg_probe_gain_sharpe_pct', 0.0)}",
            f"- Quantas mudaram de decisao vs pipeline antigo? {answers.get('changed_decision_vs_old_pipeline', 0)}",
            "",
            "## Auditoria PAPER_CANDIDATE",
            "",
            f"- PAPER_CANDIDATE deve iniciar automaticamente Paper Experimental? {((report.get('overnight_v2', {}) or {}).get('paper_candidate_should_auto_start_paper_experimental', 'SIM'))}",
            f"- PAPER_CANDIDATE sem Paper Experimental: {(((report.get('overnight_v2', {}) or {}).get('paper_candidate_promotion_rule_audit', {}) or {}).get('paper_candidate_without_experimental_count', 0))}",
            "",
            "## Respostas obrigatorias",
            "",
            f"- Estrategias pesquisadas: {answers.get('strategies_researched', 0)}",
            f"- Estrategias implementadas nesta campanha: {answers.get('implemented_in_campaign', 0)}",
            f"- Estrategias efetivamente avaliadas: {answers.get('effectively_evaluated', 0)}",
            f"- Estrategias reprovadas por desempenho: {answers.get('rejected_performance', 0)}",
            f"- Estrategias reprovadas por infraestrutura: {answers.get('rejected_infrastructure', 0)}",
            f"- Estrategias aprovadas: {answers.get('approved', 0)}",
            f"- Estrategias em Paper Trading: {answers.get('in_paper_trading', 0)}",
            f"- Estrategias em PAPER_CANDIDATE: {answers.get('classified_paper_candidate', 0)}",
            f"- Estrategias ainda pendentes: {answers.get('pending', 0)}",
            f"- Cobertura antes (%): {coverage_before.get('coverage_pct', 0.0)}",
            f"- Cobertura depois (%): {coverage_after.get('coverage_pct', 0.0)}",
            "",
            "## Validacao cientifica",
            "",
            f"- O Optimizer Probe reduziu falsos negativos? {answers.get('optimizer_probe_reduced_false_negatives', 'NAO')}",
            f"- O Optimizer Probe alterou criterios cientificos? {answers.get('optimizer_probe_altered_scientific_criteria', 'NAO')}",
            f"- O Optimizer Probe apenas deu oportunidade minima de parametrizacao antes da decisao? {answers.get('optimizer_probe_only_minimal_param_opportunity', 'SIM')}",
            f"- A camada PAPER_CANDIDATE foi habilitada sem alterar criterios cientificos? {answers.get('paper_candidate_enabled', 'SIM')}",
            "",
            "## Validacao final",
            "",
            f"- O backlog foi realmente consumido? {'SIM' if int(stages.get('implementation_real', 0)) > 0 else 'NAO'}",
            f"- Quantas estrategias passaram por implementacao real? {stages.get('implementation_real', 0)}",
            f"- Quantas chegaram ao Backtest? {stages.get('backtest_reached', 0)}",
            f"- Quantas chegaram ao Optimizer? {stages.get('optimizer_reached', 0)}",
            f"- Quantas chegaram ao Validation? {stages.get('validation_reached', 0)}",
            f"- Quantas chegaram ao Paper Qualification? {stages.get('paper_qualification_reached', 0)}",
            f"- Existe estrategia apta para Paper Trading? {'SIM' if report.get('in_paper_trading_count', 0) > 0 else 'NAO'}",
            "",
            "## Estados",
            "",
            "| Estrategia | Estado | Score fila | Penalidade |",
            "|---|---|---:|---:|",
        ]
        for row in report.get("backlog", []):
            lines.append(
                f"| {row.get('candidate_name')} | {row.get('state')} | "
                f"{float(row.get('queue_score', 0.0)):.2f} | {float(row.get('priority_penalty', 0.0)):.2f} |"
            )

        lines += [
            "",
            "## Principais motivos de reprovacao",
            "",
        ]
        for reason, count in report.get("top_rejection_reasons", []):
            lines.append(f"- {reason}: {count}")

        lines += [
            "",
            "## Resposta objetiva",
            "",
            f"Quantas estrategias aptas para Paper Trading foram encontradas? {report.get('answer', {}).get('value', 0)}",
            "",
        ]
        return "\n".join(lines)

    def _persist_checkpoint(
        self,
        run_id: str,
        payload: dict[str, Any],
        cfg: Phase13ContinuousFactoryConfig | None = None,
        report: dict[str, Any] | None = None,
        final: bool = False,
    ) -> None:
        try:
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.save_checkpoint(
                    execution_id=run_id,
                    stage="phase13_continuous_strategy_factory",
                    processed=len(payload.get("backlog", [])),
                    completed=payload.get("status") != "running",
                    payload=payload,
                )
                session.commit()
        except Exception as exc:
            logger.warning("Checkpoint persistence failed for phase13 run=%s: %s", run_id, exc)
        try:
            self._write_monitor_api(run_id, payload, cfg=cfg, report=report, final=final)
        except Exception as exc:
            logger.warning("Monitor API write failed for phase13 run=%s: %s", run_id, exc)
