from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from config.settings import settings
from risk.risk_manager import RiskManager
from research.services.phase13_continuous_strategy_factory import (
    ContinuousStrategyFactoryService,
    Phase13ContinuousFactoryConfig,
)
from strategies.factory import create_strategy
from strategies.registry import list_registered_strategies
from utils.logger import get_logger

logger = get_logger(__name__)


TERMINAL_STATES = {
    "IMPLEMENTATION_PENDING",
    "IMPLEMENTATION_IN_PROGRESS",
    "IMPLEMENTED",
    "IMPLEMENTATION_INCOMPLETE",
    "SMOKE_FAILED",
    "BACKTEST_FAILED",
    "OPTIMIZATION_FAILED",
    "VALIDATION_FAILED",
    "PAPER_APPROVED",
    "PAPER_RUNNING",
    "REJECTED_BY_PERFORMANCE",
    "REJECTED_BY_INFRASTRUCTURE",
    "INCONCLUSIVE",
    "INCONCLUSIVE_RESOURCE_LIMIT",
}


@dataclass(frozen=True)
class Phase131AuditConfig:
    symbol: str
    timeframe: str
    window_days: int = 120
    capital: float = 10_000.0
    max_bars: int = 3500
    optimizer_max_combinations: int = 15
    optimizer_workers: int = 1
    max_pending_to_process: int = 12
    mode: str = "audit"
    max_workers: int = 2
    max_strategy_runtime_seconds: int = 900
    max_optimizer_combinations_per_strategy: int = 15
    max_cpu_per_worker_pct: float = 100.0
    output_prefix: str = "phase13_1_audit_strengthening"


class Phase131AuditStrengtheningService:
    """FASE 13.1 audit and hardening layer for the Continuous Strategy Factory."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._base = ContinuousStrategyFactoryService(base_dir=base_dir)

    def run(self, cfg: Phase131AuditConfig) -> dict[str, Any]:
        if str(cfg.mode).lower() == "continuous_coverage":
            return self.run_continuous_coverage(cfg)

        now = datetime.now(tz=timezone.utc)
        state = self._load_state()
        backlog = self._reclassify_last_campaign(state)
        backlog = self._reprioritize_with_phase14(backlog)

        start_dt = now - timedelta(days=max(10, int(cfg.window_days)))
        end_dt = now

        process_count = 0
        for item in self._pending_first(backlog):
            if process_count >= max(1, int(cfg.max_pending_to_process)):
                break
            process_count += 1
            self._process_item(item, cfg, start_dt, end_dt)

        self._save_state(backlog)
        report = self._build_report(cfg, backlog)
        outputs = self._write_outputs(cfg.output_prefix, report)

        summary = {
            "status": "completed",
            "phase": "13.1",
            "processed_pending": process_count,
            "effectively_evaluated": report["answers"]["effectively_evaluated"],
            "without_implementation": report["answers"]["without_implementation"],
            "market_rejections": report["answers"]["market_rejections"],
            "eligible_for_future_implementation": report["answers"]["eligible_for_future_implementation"],
            "paper_ready_after_audit": report["answers"]["paper_ready_after_audit"],
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def run_continuous_coverage(self, cfg: Phase131AuditConfig) -> dict[str, Any]:
        campaign_started = datetime.now(tz=timezone.utc)
        campaign_t0 = perf_counter()
        now = datetime.now(tz=timezone.utc)
        state = self._load_state()
        backlog = self._reclassify_last_campaign(state)
        backlog = self._reprioritize_with_phase14(backlog)

        researched_total = int((self._latest_phase14_report() or {}).get("total_researched", len(backlog)))
        before = self._coverage_snapshot(backlog, researched_total)

        start_dt = now - timedelta(days=max(10, int(cfg.window_days)))
        end_dt = now
        pending = self._pending_coverage_queue(backlog)

        campaign: dict[str, Any] = {
            "started_at": campaign_started.isoformat(),
            "processed": 0,
            "attempts_total": 0,
            "implemented_in_campaign": 0,
            "evaluated_in_campaign": 0,
            "rejected_performance": 0,
            "rejected_infrastructure": 0,
            "inconclusive": 0,
            "paper_approved_in_campaign": 0,
            "still_pending": 0,
            "durations_seconds": [],
            "stop_reason": "backlog_exhausted",
            "learning_feed": [],
        }

        active: dict[Any, dict[str, Any]] = {}
        stop_dispatch = False
        max_workers = max(1, int(cfg.max_workers))
        queue = deque(pending)
        attempts_by_candidate: dict[str, int] = {}
        max_attempts_per_candidate = 2
        progress_in_last_cycle = False
        pass_size_reference = max(1, len(queue))
        cycle_processed = 0
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="phase13_2_worker") as pool:
            while queue or active:
                while (not stop_dispatch) and queue and len(active) < max_workers:
                    item = queue.popleft()
                    previous_state = str(item.get("state", ""))
                    item["campaign_previous_state"] = previous_state
                    fut = pool.submit(self._process_item_coverage_worker, item, cfg, start_dt, end_dt)
                    active[fut] = item

                if not active:
                    break

                done, _ = wait(set(active.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    item = active.pop(fut)
                    campaign["attempts_total"] += 1
                    try:
                        outcome = fut.result()
                    except Exception as exc:
                        item["state"] = "INCONCLUSIVE_RESOURCE_LIMIT"
                        item["state_reason"] = f"worker_exception:{exc}"
                        outcome = {
                            "elapsed_seconds": 0.0,
                            "was_implemented": False,
                            "was_evaluated": False,
                            "final_state": item.get("state"),
                        }

                    campaign["processed"] += 1
                    campaign["durations_seconds"].append(float(outcome.get("elapsed_seconds", 0.0)))
                    if bool(outcome.get("was_implemented")):
                        campaign["implemented_in_campaign"] += 1
                    if bool(outcome.get("was_evaluated")):
                        campaign["evaluated_in_campaign"] += 1

                    final_state = str(outcome.get("final_state", ""))
                    if final_state in {"REJECTED_BY_PERFORMANCE", "BACKTEST_FAILED", "VALIDATION_FAILED"}:
                        campaign["rejected_performance"] += 1
                    if final_state in {"REJECTED_BY_INFRASTRUCTURE", "SMOKE_FAILED", "OPTIMIZATION_FAILED"}:
                        campaign["rejected_infrastructure"] += 1
                    if final_state in {"INCONCLUSIVE", "INCONCLUSIVE_RESOURCE_LIMIT"}:
                        campaign["inconclusive"] += 1
                    if final_state in {"PAPER_APPROVED", "PAPER_RUNNING"}:
                        campaign["paper_approved_in_campaign"] += 1

                    campaign["learning_feed"].append(self._learning_row(item, cfg))
                    if bool(outcome.get("was_evaluated")):
                        progress_in_last_cycle = True

                    candidate_key = self._canon(str(item.get("candidate_name", "")))
                    if final_state in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}:
                        attempts_by_candidate[candidate_key] = attempts_by_candidate.get(candidate_key, 0) + 1
                        if attempts_by_candidate[candidate_key] < max_attempts_per_candidate:
                            queue.append(item)

                    cycle_processed += 1
                    if cycle_processed >= pass_size_reference:
                        if (not progress_in_last_cycle) and queue and (not self._has_paper_ready(backlog)):
                            campaign["stop_reason"] = "no_progress_blocked_queue"
                            stop_dispatch = True
                            queue.clear()
                        progress_in_last_cycle = False
                        cycle_processed = 0
                        pass_size_reference = max(1, len(queue))

                    if self._has_paper_ready(backlog):
                        campaign["stop_reason"] = "paper_approved"
                        stop_dispatch = True
                        queue.clear()
                    elif not self._pending_coverage_queue(backlog):
                        campaign["stop_reason"] = "all_eligible_evaluated"
                        stop_dispatch = True
                        queue.clear()

        campaign["still_pending"] = len(self._pending_coverage_queue(backlog))
        campaign["ended_at"] = datetime.now(tz=timezone.utc).isoformat()
        campaign["total_campaign_seconds"] = round(perf_counter() - campaign_t0, 3)

        after = self._coverage_snapshot(backlog, researched_total)
        self._persist_learning_feed(campaign.get("learning_feed", []))
        self._save_state(backlog)
        report = self._build_continuous_coverage_report(cfg, backlog, before, after, campaign)
        outputs = self._write_outputs(cfg.output_prefix, report)
        summary = report.get("summary", {})
        return {"summary": summary, "report": report, "outputs": outputs}

    def _process_item_coverage_worker(
        self,
        item: dict[str, Any],
        cfg: Phase131AuditConfig,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        t0 = perf_counter()
        previous_state = str(item.get("campaign_previous_state") or item.get("state") or "")
        self._attach_phase14_spec(item)
        allowed_optimizer_workers = max(1, int(max(1.0, float(cfg.max_cpu_per_worker_pct)) // 25))

        local_cfg = Phase131AuditConfig(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            window_days=cfg.window_days,
            capital=cfg.capital,
            max_bars=cfg.max_bars,
            optimizer_max_combinations=min(
                int(cfg.optimizer_max_combinations),
                max(1, int(cfg.max_optimizer_combinations_per_strategy)),
            ),
            optimizer_workers=min(max(1, int(cfg.optimizer_workers)), allowed_optimizer_workers),
            max_pending_to_process=cfg.max_pending_to_process,
            mode=cfg.mode,
            max_workers=cfg.max_workers,
            max_strategy_runtime_seconds=cfg.max_strategy_runtime_seconds,
            max_optimizer_combinations_per_strategy=cfg.max_optimizer_combinations_per_strategy,
            max_cpu_per_worker_pct=cfg.max_cpu_per_worker_pct,
            output_prefix=cfg.output_prefix,
        )

        self._process_item(
            item,
            local_cfg,
            start_dt,
            end_dt,
            allow_proxy=False,
            start_perf=t0,
            max_runtime_seconds=max(30, int(cfg.max_strategy_runtime_seconds)),
        )
        item["resource_policy"] = {
            "max_cpu_per_worker_pct": float(cfg.max_cpu_per_worker_pct),
            "optimizer_workers_effective": int(local_cfg.optimizer_workers),
            "max_strategy_runtime_seconds": int(cfg.max_strategy_runtime_seconds),
            "max_optimizer_combinations_per_strategy": int(cfg.max_optimizer_combinations_per_strategy),
        }
        final_state = str(item.get("state", ""))
        was_implemented = final_state not in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}
        was_evaluated = final_state in {
            "SMOKE_FAILED",
            "BACKTEST_FAILED",
            "OPTIMIZATION_FAILED",
            "VALIDATION_FAILED",
            "REJECTED_BY_PERFORMANCE",
            "REJECTED_BY_INFRASTRUCTURE",
            "PAPER_APPROVED",
            "PAPER_RUNNING",
            "INCONCLUSIVE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
        }
        return {
            "elapsed_seconds": round(perf_counter() - t0, 3),
            "previous_state": previous_state,
            "final_state": final_state,
            "was_implemented": was_implemented,
            "was_evaluated": was_evaluated,
        }

    def _process_item(
        self,
        item: dict[str, Any],
        cfg: Phase131AuditConfig,
        start_dt: datetime,
        end_dt: datetime,
        allow_proxy: bool = True,
        start_perf: float | None = None,
        max_runtime_seconds: int | None = None,
    ) -> None:
        if item.get("state") not in {
            "IMPLEMENTATION_PENDING",
            "INCONCLUSIVE",
            "IMPLEMENTATION_INCOMPLETE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
        }:
            return

        item["state"] = "IMPLEMENTATION_IN_PROGRESS"
        item["state_reason"] = "starting_faithful_implementation"
        item["updated_at"] = datetime.now(tz=timezone.utc).isoformat()

        impl = self._attempt_faithful_implementation(item, allow_proxy=allow_proxy)
        item["implementation_audit"] = impl
        if not impl.get("implemented", False):
            item["state"] = "IMPLEMENTATION_PENDING"
            item["state_reason"] = str(impl.get("reason", "implementation_not_available"))
            return

        fidelity = self._fidelity_audit(item, impl)
        item["fidelity_audit"] = fidelity
        if fidelity.get("answer") == "NAO":
            item["state"] = "IMPLEMENTATION_INCOMPLETE"
            item["state_reason"] = "implementation_not_faithful"
            return

        item["state"] = "IMPLEMENTED"
        item["state_reason"] = "implemented_faithfully"

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        run_cfg = Phase13ContinuousFactoryConfig(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            window_days=cfg.window_days,
            capital=cfg.capital,
            max_bars=cfg.max_bars,
            optimizer_max_combinations=cfg.optimizer_max_combinations,
            optimizer_workers=cfg.optimizer_workers,
        )
        market_df = self._base._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt, cfg.max_bars)
        if market_df.empty:
            item["state"] = "REJECTED_BY_INFRASTRUCTURE"
            item["state_reason"] = "no_market_data"
            return

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        strategy_name = str(item.get("platform_strategy_name") or "")
        smoke = self._base._run_smoke(strategy_name, market_df, run_cfg)
        item["smoke"] = smoke
        risk_audit = self._risk_numerical_audit(smoke, item)
        if risk_audit:
            item["risk_precision_audit"] = risk_audit
        if not smoke.get("passed", False):
            item["state"] = "SMOKE_FAILED"
            item["state_reason"] = ";".join(smoke.get("errors", [])) or "smoke_failed"
            return

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        backtest = self._base._run_backtest(strategy_name, market_df, run_cfg)
        item["backtest"] = backtest
        early = self._base._early_stop(backtest)
        item["early_stop"] = early
        if early.get("triggered", False):
            matrix = self._context_matrix(strategy_name, run_cfg)
            item["context_matrix"] = matrix
            if matrix.get("context_dependent", False):
                item["state"] = "INCONCLUSIVE"
                item["state_reason"] = "CONTEXT_DEPENDENT"
                return
            item["state"] = "REJECTED_BY_PERFORMANCE"
            item["state_reason"] = ";".join(early.get("reasons", [])) or "early_stop"
            return

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        optimizer = self._base._run_optimizer(strategy_name, run_cfg, start_dt, end_dt)
        item["optimizer"] = optimizer
        if not optimizer.get("top_results"):
            item["state"] = "OPTIMIZATION_FAILED"
            item["state_reason"] = "no_optimizer_results"
            return

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        validation = self._base._run_validation(strategy_name, run_cfg, start_dt, end_dt, optimizer)
        item["validation"] = validation
        if not validation.get("approved", False):
            matrix = self._context_matrix(strategy_name, run_cfg)
            item["context_matrix"] = matrix
            if matrix.get("context_dependent", False):
                item["state"] = "INCONCLUSIVE"
                item["state_reason"] = "CONTEXT_DEPENDENT"
                return
            item["state"] = "VALIDATION_FAILED"
            item["state_reason"] = ";".join(validation.get("reasons", [])) or "validation_failed"
            return

        if self._resource_limit_reached(item, start_perf, max_runtime_seconds):
            return

        paper = self._base._prepare_paper_trading(strategy_name, market_df, run_cfg)
        item["paper"] = paper
        if str(paper.get("status", "")).lower() == "running":
            item["state"] = "PAPER_RUNNING"
            item["state_reason"] = "approved_and_running_in_paper"
        else:
            item["state"] = "PAPER_APPROVED"
            item["state_reason"] = "approved_for_paper"

    def _attempt_faithful_implementation(self, item: dict[str, Any], allow_proxy: bool = True) -> dict[str, Any]:
        existing = {self._canon(str(s.get("name", ""))): str(s.get("name")) for s in list_registered_strategies()}
        candidate = str(item.get("candidate_name", ""))
        ckey = self._canon(candidate)

        missing = self._detect_missing_indicators(item)
        if missing:
            return {
                "implemented": False,
                "mode": "none",
                "reason": "missing_indicator_implementation_required",
                "missing_indicators": missing,
            }

        if ckey in existing:
            item["platform_strategy_name"] = existing[ckey]
            return {"implemented": True, "mode": "native_exact", "strategy": existing[ckey], "reason": "existing_exact_implementation"}

        if not allow_proxy:
            return {
                "implemented": False,
                "mode": "none",
                "reason": "no_faithful_implementation_available_without_proxy",
            }

        aliases = {
            "supertrend": "SuperTrendV1",
            "utbot": "SuperTrendV1",
            "wavetrend": "MeanReversionV1",
            "qqe": "TrendV2",
            "sslhybrid": "TradeOutcomeNextGenV1",
            "chandelierexittrend": "TradeOutcomeNextGenV1.1",
            "donchianturtlebreakout": "BreakoutV1",
            "donchianturtlecrypto": "BreakoutV1",
            "macdsignalmomentum": "TrendV2",
            "rsi2meanreversion": "MeanReversionV1",
            "adxtrendcontinuation": "TrendV1",
            "atrvolatilitycompressionbreak": "TradeOutcomeNextGenV1.1",
        }
        mapped = aliases.get(ckey)
        if mapped and self._canon(mapped) in existing:
            item["platform_strategy_name"] = existing[self._canon(mapped)]
            return {"implemented": True, "mode": "mapped_family_proxy", "strategy": existing[self._canon(mapped)], "reason": "mapped_from_research_reference"}

        return {"implemented": False, "mode": "none", "reason": "no_faithful_implementation_available"}

    def _fidelity_audit(self, item: dict[str, Any], impl: dict[str, Any]) -> dict[str, Any]:
        mode = str(impl.get("mode", "none"))
        if mode == "native_exact":
            return {
                "answer": "SIM",
                "differences": [],
                "missing_indicators": [],
                "missing_filters": [],
                "simplifications": [],
                "expected_impact": "none",
            }

        return {
            "answer": "NAO",
            "differences": ["reference strategy mapped to platform proxy"],
            "missing_indicators": list(item.get("indicators", [])),
            "missing_filters": ["original entry/exit confirmation may differ from proxy"],
            "simplifications": ["family-level proxy used for implementation bootstrap"],
            "expected_impact": "may under/overestimate edge versus original reference",
        }

    def _context_matrix(self, strategy_name: str, cfg: Phase13ContinuousFactoryConfig) -> dict[str, Any]:
        contexts = [
            ("BTC/USDT", "5m"),
            ("BTC/USDT", "1h"),
            ("ETH/USDT", "5m"),
            ("ETH/USDT", "1h"),
        ]
        rows: list[dict[str, Any]] = []
        pfs: list[float] = []

        for symbol, timeframe in contexts:
            end_dt = datetime.now(tz=timezone.utc)
            start_dt = end_dt - timedelta(days=max(30, int(cfg.window_days // 2)))
            df = self._base._load_df(symbol, timeframe, start_dt, end_dt, cfg.max_bars)
            if df.empty:
                rows.append({"symbol": symbol, "timeframe": timeframe, "status": "no_data"})
                continue
            local_cfg = Phase13ContinuousFactoryConfig(
                symbol=symbol,
                timeframe=timeframe,
                window_days=cfg.window_days,
                capital=cfg.capital,
                max_bars=cfg.max_bars,
                optimizer_max_combinations=cfg.optimizer_max_combinations,
                optimizer_workers=cfg.optimizer_workers,
            )
            try:
                bt = self._base._run_backtest(strategy_name, df, local_cfg)
                pf = float(bt.get("profit_factor", 0.0))
                pfs.append(pf)
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "status": "ok",
                        "profit_factor": pf,
                        "sharpe": float(bt.get("sharpe", 0.0)),
                        "expectancy": float(bt.get("expectancy", 0.0)),
                    }
                )
            except Exception as exc:
                rows.append({"symbol": symbol, "timeframe": timeframe, "status": f"error:{exc}"})

        context_dependent = False
        if pfs:
            if max(pfs) - min(pfs) >= 0.8:
                context_dependent = True
            if (max(pfs) >= 1.2 and min(pfs) < 0.8):
                context_dependent = True

        return {
            "rows": rows,
            "context_dependent": context_dependent,
            "evidence": "high variance across minimal context matrix" if context_dependent else "no significant context divergence",
        }

    def _risk_numerical_audit(self, smoke: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
        errors = [str(x) for x in smoke.get("errors", [])]
        rr_errors = [x for x in errors if "Risk/reward ratio" in x and "minimum" in x]
        if not rr_errors:
            return None

        message = rr_errors[0]
        rr_raw = None
        rr_min = float(settings.risk.risk_reward_ratio)
        strategy_name = str(item.get("platform_strategy_name") or "")
        if strategy_name:
            try:
                rr_override = RiskManager.resolve_min_risk_reward_ratio(create_strategy(strategy_name))
                if rr_override is not None and rr_override > 0.0:
                    rr_min = float(rr_override)
            except Exception:
                pass
        first = (item.get("smoke") or {}).get("first_signal") if isinstance(item.get("smoke"), dict) else None
        if isinstance(first, dict):
            try:
                entry = float(first.get("price"))
                stop = float(first.get("stop_loss"))
                tp = float(first.get("take_profit"))
                risk = entry - stop
                reward = tp - entry
                rr_raw = reward / risk if risk > 0 else None
            except Exception:
                rr_raw = None

        warning = False
        if rr_raw is not None and rr_raw < rr_min and abs(rr_raw - rr_min) <= 0.02:
            warning = True

        return {
            "rr_message": message,
            "rr_minimum": rr_min,
            "rr_raw": rr_raw,
            "rr_display_rounded": round(rr_raw, 2) if rr_raw is not None else None,
            "exact_rejection_reason": message,
            "classification": "NUMERICAL_PRECISION_WARNING" if warning else "RISK_RULE_REJECTION",
        }

    def _reclassify_last_campaign(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        latest = self._latest_phase13_report()
        backlog = state.get("backlog", [])
        if (not backlog) and latest:
            backlog = latest.get("backlog", backlog)

        normalized: list[dict[str, Any]] = []
        for row in backlog:
            item = dict(row)
            item.setdefault("state_reason", "")
            item.setdefault("updated_at", datetime.now(tz=timezone.utc).isoformat())

            old_state = str(item.get("state", "")).lower()
            reason = str(item.get("rejection_reason", ""))
            stage = str(item.get("rejection_stage", ""))

            if old_state in {"in_paper_trading"}:
                item["state"] = "PAPER_RUNNING"
                item["state_reason"] = "already_running_in_paper"
            elif old_state in {"approved"}:
                item["state"] = "PAPER_APPROVED"
                item["state_reason"] = "already_approved"
            elif "not_implemented_in_platform" in reason:
                item["state"] = "IMPLEMENTATION_PENDING"
                item["state_reason"] = "missing_implementation"
            elif stage == "implementation":
                item["state"] = "REJECTED_BY_INFRASTRUCTURE"
                item["state_reason"] = reason or "implementation_failure"
            elif stage == "smoke_test":
                item["state"] = "SMOKE_FAILED"
                item["state_reason"] = reason or "smoke_failed"
            elif stage == "validation":
                item["state"] = "VALIDATION_FAILED"
                item["state_reason"] = reason or "validation_failed"
            elif stage == "early_stop":
                item["state"] = "REJECTED_BY_PERFORMANCE"
                item["state_reason"] = reason or "early_stop"
            elif stage == "backtest":
                item["state"] = "BACKTEST_FAILED"
                item["state_reason"] = reason or "backtest_failed"
            elif old_state in {"queued", "researched", "awaiting_evaluation"}:
                item["state"] = "IMPLEMENTATION_PENDING"
                item["state_reason"] = "pending_implementation_and_audit"
            else:
                item["state"] = "INCONCLUSIVE"
                item["state_reason"] = reason or "needs_manual_audit"

            if item["state"] not in TERMINAL_STATES:
                item["state"] = "INCONCLUSIVE"
            normalized.append(item)

        return normalized

    def _reprioritize_with_phase14(self, backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        phase14 = self._latest_phase14_report()
        low_categories: set[str] = set()
        if phase14:
            dist = phase14.get("distribution_by_category", {})
            if isinstance(dist, dict) and dist:
                min_count = min(int(v) for v in dist.values())
                for k, v in dist.items():
                    if int(v) == min_count:
                        low_categories.add(self._canon(str(k)))

        for item in backlog:
            state = str(item.get("state", ""))
            family = self._canon(str(item.get("family", "")))
            priority = 100.0
            if state == "IMPLEMENTATION_PENDING":
                priority -= 60.0
            if family in low_categories:
                priority -= 30.0
            if state in {"INCONCLUSIVE"}:
                priority -= 20.0
            item["phase13_1_priority"] = priority

        return sorted(backlog, key=lambda x: float(x.get("phase13_1_priority", 100.0)))

    def _pending_first(self, backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(backlog, key=lambda x: float(x.get("phase13_1_priority", 100.0)))
        return [x for x in ordered if x.get("state") in {"IMPLEMENTATION_PENDING", "INCONCLUSIVE"}]

    def _build_report(self, cfg: Phase131AuditConfig, backlog: list[dict[str, Any]]) -> dict[str, Any]:
        implemented = [x for x in backlog if x.get("state") == "IMPLEMENTED"]
        implementation_incomplete = [x for x in backlog if x.get("state") == "IMPLEMENTATION_INCOMPLETE"]
        implementation_pending = [x for x in backlog if x.get("state") == "IMPLEMENTATION_PENDING"]
        infra_rejected = [x for x in backlog if x.get("state") in {"REJECTED_BY_INFRASTRUCTURE", "SMOKE_FAILED", "OPTIMIZATION_FAILED"}]
        perf_rejected = [x for x in backlog if x.get("state") in {"REJECTED_BY_PERFORMANCE", "BACKTEST_FAILED", "VALIDATION_FAILED"}]
        paper_approved = [x for x in backlog if x.get("state") == "PAPER_APPROVED"]
        paper_running = [x for x in backlog if x.get("state") == "PAPER_RUNNING"]
        context_dependent = [x for x in backlog if str(x.get("state_reason", "")).upper() == "CONTEXT_DEPENDENT"]
        inconclusive = [x for x in backlog if x.get("state") == "INCONCLUSIVE"]

        cause_stats = self._cause_stats(backlog)
        family_stats = self._family_stats(backlog)

        effectively_evaluated = len(perf_rejected) + len(paper_approved) + len(paper_running)
        without_implementation = len(implementation_pending)
        market_rejections = len(perf_rejected)
        eligible_future = len(implementation_pending) + len(implementation_incomplete) + len(inconclusive)
        paper_ready = len(paper_approved) + len(paper_running)

        return {
            "phase": "13.1",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "states_catalog": sorted(TERMINAL_STATES),
            "counts": {
                "implemented": len(implemented),
                "implementation_incomplete": len(implementation_incomplete),
                "implementation_pending": len(implementation_pending),
                "rejected_by_infrastructure": len(infra_rejected),
                "rejected_by_performance": len(perf_rejected),
                "paper_approved": len(paper_approved),
                "paper_running": len(paper_running),
                "context_dependent": len(context_dependent),
                "inconclusive": len(inconclusive),
            },
            "learning": {
                "rejections_by_cause": cause_stats,
                "family_statistics": family_stats,
            },
            "backlog": backlog,
            "answers": {
                "effectively_evaluated": effectively_evaluated,
                "without_implementation": without_implementation,
                "market_rejections": market_rejections,
                "eligible_for_future_implementation": eligible_future,
                "paper_ready_after_audit": paper_ready,
            },
        }

    def _build_continuous_coverage_report(
        self,
        cfg: Phase131AuditConfig,
        backlog: list[dict[str, Any]],
        before: dict[str, Any],
        after: dict[str, Any],
        campaign: dict[str, Any],
    ) -> dict[str, Any]:
        perf_rejected = len([x for x in backlog if x.get("state") in {"REJECTED_BY_PERFORMANCE", "BACKTEST_FAILED", "VALIDATION_FAILED"}])
        infra_rejected = len([x for x in backlog if x.get("state") in {"REJECTED_BY_INFRASTRUCTURE", "SMOKE_FAILED", "OPTIMIZATION_FAILED"}])
        inconclusive = len([x for x in backlog if x.get("state") in {"INCONCLUSIVE", "INCONCLUSIVE_RESOURCE_LIMIT"}])
        paper_ready = len([x for x in backlog if x.get("state") in {"PAPER_APPROVED", "PAPER_RUNNING"}])
        pending = len([x for x in backlog if x.get("state") in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}])
        avg_seconds = 0.0
        durations = campaign.get("durations_seconds", [])
        if durations:
            avg_seconds = float(sum(float(x) for x in durations)) / float(len(durations))

        report = {
            "phase": "13.2",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "mode": "continuous_coverage",
            "governance": {
                "used_existing_pipeline": True,
                "changed_scientific_criteria": False,
                "changed_optimizer": False,
                "changed_validation": False,
                "changed_paper": False,
                "changed_risk_manager": False,
            },
            "campaign": campaign,
            "coverage_before": before,
            "coverage_after": after,
            "counts_final": {
                "strategies_researched": after.get("strategies_researched", 0),
                "strategies_implemented": after.get("strategies_implemented", 0),
                "strategies_effectively_evaluated": after.get("strategies_effectively_evaluated", 0),
                "strategies_pending": pending,
                "rejected_performance": perf_rejected,
                "rejected_infrastructure": infra_rejected,
                "inconclusive": inconclusive,
                "paper_approved": paper_ready,
            },
            "answers": {
                "strategies_researched": after.get("strategies_researched", 0),
                "strategies_implemented_in_campaign": int(campaign.get("implemented_in_campaign", 0)),
                "strategies_evaluated_in_campaign": int(campaign.get("evaluated_in_campaign", 0)),
                "attempts_total": int(campaign.get("attempts_total", 0)),
                "strategies_rejected_performance": int(campaign.get("rejected_performance", 0)),
                "strategies_rejected_infrastructure": int(campaign.get("rejected_infrastructure", 0)),
                "strategies_inconclusive": int(campaign.get("inconclusive", 0)),
                "strategies_approved_for_paper": int(campaign.get("paper_approved_in_campaign", 0)),
                "strategies_still_pending": int(campaign.get("still_pending", 0)),
                "coverage_before_pct": before.get("coverage_pct", 0.0),
                "coverage_after_pct": after.get("coverage_pct", 0.0),
                "average_time_per_strategy_seconds": round(avg_seconds, 3),
                "total_campaign_seconds": float(campaign.get("total_campaign_seconds", 0.0)),
                "newly_implemented": int(campaign.get("implemented_in_campaign", 0)),
                "newly_effectively_evaluated": int(campaign.get("evaluated_in_campaign", 0)),
                "advanced_to_paper": int(campaign.get("paper_approved_in_campaign", 0)),
                "continues_pending": int(campaign.get("still_pending", 0)),
                "has_strategy_ready_for_continuous_operation": "SIM" if paper_ready > 0 else "NAO",
                "queue_stop_reason": str(campaign.get("stop_reason", "backlog_exhausted")),
            },
            "backlog": backlog,
        }
        report["summary"] = {
            "status": "completed",
            "phase": "13.2",
            "processed": int(campaign.get("processed", 0)),
            "implemented_in_campaign": int(campaign.get("implemented_in_campaign", 0)),
            "evaluated_in_campaign": int(campaign.get("evaluated_in_campaign", 0)),
            "paper_approved_in_campaign": int(campaign.get("paper_approved_in_campaign", 0)),
            "pending_after_campaign": int(campaign.get("still_pending", 0)),
            "coverage_before_pct": before.get("coverage_pct", 0.0),
            "coverage_after_pct": after.get("coverage_pct", 0.0),
            "stop_reason": str(campaign.get("stop_reason", "backlog_exhausted")),
        }
        return report

    def _coverage_snapshot(self, backlog: list[dict[str, Any]], researched_total: int) -> dict[str, Any]:
        implemented = 0
        effectively_evaluated = 0
        pending = 0
        for item in backlog:
            state = str(item.get("state", ""))
            if state not in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}:
                implemented += 1
            if state in {
                "REJECTED_BY_PERFORMANCE",
                "REJECTED_BY_INFRASTRUCTURE",
                "SMOKE_FAILED",
                "BACKTEST_FAILED",
                "OPTIMIZATION_FAILED",
                "VALIDATION_FAILED",
                "PAPER_APPROVED",
                "PAPER_RUNNING",
                "INCONCLUSIVE",
                "INCONCLUSIVE_RESOURCE_LIMIT",
            }:
                effectively_evaluated += 1
            if state in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}:
                pending += 1

        denom = max(1, int(researched_total))
        coverage_pct = round((float(implemented) / float(denom)) * 100.0, 2)
        return {
            "strategies_researched": int(researched_total),
            "strategies_implemented": int(implemented),
            "strategies_effectively_evaluated": int(effectively_evaluated),
            "strategies_pending": int(pending),
            "coverage_pct": coverage_pct,
        }

    def _pending_coverage_queue(self, backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(backlog, key=lambda x: float(x.get("phase13_1_priority", 100.0)))
        return [x for x in ordered if x.get("state") in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"}]

    def _has_paper_ready(self, backlog: list[dict[str, Any]]) -> bool:
        return any(str(x.get("state", "")) in {"PAPER_APPROVED", "PAPER_RUNNING"} for x in backlog)

    def _resource_limit_reached(
        self,
        item: dict[str, Any],
        start_perf: float | None,
        max_runtime_seconds: int | None,
    ) -> bool:
        if start_perf is None or max_runtime_seconds is None:
            return False
        elapsed = perf_counter() - float(start_perf)
        if elapsed <= float(max_runtime_seconds):
            return False
        item["state"] = "INCONCLUSIVE_RESOURCE_LIMIT"
        item["state_reason"] = f"resource_limit:max_runtime_seconds={int(max_runtime_seconds)}"
        item["resource_limit"] = {
            "elapsed_seconds": round(elapsed, 3),
            "max_runtime_seconds": int(max_runtime_seconds),
        }
        return True

    def _attach_phase14_spec(self, item: dict[str, Any]) -> None:
        phase14 = self._latest_phase14_report() or {}
        pool: list[dict[str, Any]] = []
        for key in ("classified", "top20", "top10", "top5"):
            value = phase14.get(key)
            if isinstance(value, list):
                pool.extend([x for x in value if isinstance(x, dict)])
        ckey = self._canon(str(item.get("candidate_name", "")))
        for row in pool:
            name = str(row.get("name") or row.get("strategy_name") or row.get("candidate_name") or "")
            if self._canon(name) == ckey:
                item["phase14_spec"] = row
                return

    def _learning_row(self, item: dict[str, Any], cfg: Phase131AuditConfig) -> dict[str, Any]:
        context = f"{cfg.symbol} {cfg.timeframe}"
        matrix = item.get("context_matrix") if isinstance(item.get("context_matrix"), dict) else {}
        if matrix:
            context = str(matrix.get("evidence") or context)
        return {
            "candidate_name": item.get("candidate_name"),
            "family": item.get("family"),
            "indicators": item.get("indicators", []),
            "result_state": item.get("state"),
            "reason": item.get("state_reason"),
            "context": context,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    def _cause_stats(self, backlog: list[dict[str, Any]]) -> dict[str, int]:
        stats = {
            "structure": 0,
            "implementation": 0,
            "market": 0,
            "risco": 0,
            "timeout": 0,
            "contexto": 0,
        }
        for row in backlog:
            state = str(row.get("state", ""))
            reason = str(row.get("state_reason", "")).lower()
            if state in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_IN_PROGRESS", "IMPLEMENTATION_INCOMPLETE"}:
                stats["implementation"] += 1
            elif state in {"REJECTED_BY_INFRASTRUCTURE", "SMOKE_FAILED", "OPTIMIZATION_FAILED"}:
                stats["structure"] += 1
            elif state in {"REJECTED_BY_PERFORMANCE", "BACKTEST_FAILED", "VALIDATION_FAILED"}:
                stats["market"] += 1
            if "risk" in reason or "rr" in reason:
                stats["risco"] += 1
            if "timeout" in reason:
                stats["timeout"] += 1
            if "context_dependent" in reason:
                stats["contexto"] += 1
        return stats

    def _family_stats(self, backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        families = ["tendencia", "reversao", "breakout", "momentum", "volatilidade", "hibridas"]
        out: list[dict[str, Any]] = []
        for fam in families:
            members = [x for x in backlog if self._canon(str(x.get("family", ""))) == fam]
            if not members:
                out.append({"family": fam, "total": 0, "paper": 0, "performance_rejections": 0})
                continue
            paper = len([x for x in members if x.get("state") in {"PAPER_APPROVED", "PAPER_RUNNING"}])
            perf = len([x for x in members if x.get("state") in {"REJECTED_BY_PERFORMANCE", "BACKTEST_FAILED", "VALIDATION_FAILED"}])
            out.append({"family": fam, "total": len(members), "paper": paper, "performance_rejections": perf})
        return out

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}_states.csv"
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
                        "state_reason",
                        "phase13_1_priority",
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

    def _to_markdown(self, report: dict[str, Any]) -> str:
        if str(report.get("phase")) == "13.2":
            answers = report.get("answers", {})
            before = report.get("coverage_before", {})
            after = report.get("coverage_after", {})
            summary = report.get("summary", {})
            lines = [
                "# FASE 13.2 - Execucao Continua de Cobertura",
                "",
                "## Resumo da campanha",
                "",
                f"- Estrategias pesquisadas: {answers.get('strategies_researched', 0)}",
                f"- Estrategias implementadas nesta campanha: {answers.get('strategies_implemented_in_campaign', 0)}",
                f"- Estrategias avaliadas nesta campanha: {answers.get('strategies_evaluated_in_campaign', 0)}",
                f"- Reprovadas por desempenho: {answers.get('strategies_rejected_performance', 0)}",
                f"- Reprovadas por infraestrutura: {answers.get('strategies_rejected_infrastructure', 0)}",
                f"- Inconclusivas: {answers.get('strategies_inconclusive', 0)}",
                f"- Aprovadas para Paper Trading: {answers.get('strategies_approved_for_paper', 0)}",
                f"- Ainda pendentes: {answers.get('strategies_still_pending', 0)}",
                "",
                "## Cobertura",
                "",
                f"- Cobertura antes: {before.get('coverage_pct', 0.0)}%",
                f"- Cobertura depois: {after.get('coverage_pct', 0.0)}%",
                "",
                "## Operacao",
                "",
                f"- Tempo medio por estrategia (s): {answers.get('average_time_per_strategy_seconds', 0.0)}",
                f"- Tempo total da campanha (s): {answers.get('total_campaign_seconds', 0.0)}",
                f"- Motivo de parada: {summary.get('stop_reason', 'backlog_exhausted')}",
                f"- Existe estrategia apta para operacao continua? {answers.get('has_strategy_ready_for_continuous_operation', 'NAO')}",
                "",
            ]
            return "\n".join(lines)

        counts = report.get("counts", {})
        answers = report.get("answers", {})
        lines = [
            "# FASE 13.1 - Auditoria e Fortalecimento",
            "",
            "## Resumo",
            "",
            f"- Implementadas: {counts.get('implemented', 0)}",
            f"- Implementadas parcialmente: {counts.get('implementation_incomplete', 0)}",
            f"- Pendentes de implementacao: {counts.get('implementation_pending', 0)}",
            f"- Reprovadas por infraestrutura: {counts.get('rejected_by_infrastructure', 0)}",
            f"- Reprovadas por desempenho: {counts.get('rejected_by_performance', 0)}",
            f"- Aprovadas para paper: {counts.get('paper_approved', 0)}",
            f"- Em paper: {counts.get('paper_running', 0)}",
            f"- Dependentes de contexto: {counts.get('context_dependent', 0)}",
            f"- Inconclusivas: {counts.get('inconclusive', 0)}",
            "",
            "## Respostas objetivas",
            "",
            f"- Quantas estrategias foram efetivamente avaliadas? {answers.get('effectively_evaluated', 0)}",
            f"- Quantas estavam apenas sem implementacao? {answers.get('without_implementation', 0)}",
            f"- Quantas reprovaram por desempenho de mercado? {answers.get('market_rejections', 0)}",
            f"- Quantas permanecem elegiveis para implementacao futura? {answers.get('eligible_for_future_implementation', 0)}",
            f"- Quantas estrategias aptas para Paper Trading existem apos a auditoria? {answers.get('paper_ready_after_audit', 0)}",
            "",
        ]
        return "\n".join(lines)

    def _latest_phase13_report(self) -> dict[str, Any] | None:
        files = sorted(self._results_dir.glob("phase13_continuous_strategy_factory_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception:
            return None

    def _available_indicators_catalog(self) -> set[str]:
        return {
            "atr",
            "ema",
            "sma",
            "rsi",
            "macd",
            "bollinger",
            "bollingerbands",
            "adx",
            "supertrend",
            "donchian",
            "stochastic",
            "volume",
            "vwap",
            "ichimoku",
            "cci",
        }

    def _detect_missing_indicators(self, item: dict[str, Any]) -> list[str]:
        source = item.get("phase14_spec") if isinstance(item.get("phase14_spec"), dict) else item
        raw = source.get("indicators", []) if isinstance(source, dict) else []
        indicators: list[str] = []
        if isinstance(raw, list):
            for value in raw:
                if isinstance(value, dict):
                    name = str(value.get("name") or value.get("indicator") or "").strip()
                else:
                    name = str(value).strip()
                if name:
                    indicators.append(name)

        if not indicators:
            return []

        known = self._available_indicators_catalog()
        missing: list[str] = []
        for name in indicators:
            c = self._canon(name)
            if c not in known:
                missing.append(name)
        return sorted(list(set(missing)))

    def _latest_phase14_report(self) -> dict[str, Any] | None:
        files = sorted(self._results_dir.glob("phase14_market_intelligence_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception:
            return None

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
            pass
        return {"backlog": [], "rejection_knowledge": []}

    def _save_state(self, backlog: list[dict[str, Any]]) -> None:
        payload = self._load_state()
        payload["backlog"] = backlog
        payload["phase13_1_last_update"] = datetime.now(tz=timezone.utc).isoformat()
        self._state_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _persist_learning_feed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = self._load_state()
        existing = payload.get("rejection_knowledge", [])
        if not isinstance(existing, list):
            existing = []
        existing.extend(rows)
        payload["rejection_knowledge"] = existing[-1000:]
        payload["phase13_1_last_update"] = datetime.now(tz=timezone.utc).isoformat()
        self._state_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _canon(self, value: str) -> str:
        return value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
