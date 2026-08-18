from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.repositories import CandleRepository
from execution.hypothesis_runtime import (
    HypothesisApprovedContext,
    HypothesisGateConfig,
    hypothesis_gate_config_from_payload,
    wrap_strategy_with_hypothesis,
)
from optimizer.optimization_result import OptimizationResult
from paper_trading.edge_drift_monitor import EdgeDriftContext
from paper_trading.specialized_campaign import SpecializedCampaignConfig, SpecializedPaperCampaignService
from paper_trading.specialized_validation import (
    SpecializedBaseline,
    SpecializedContext,
    SpecializedPaperValidationConfig,
    SpecializedPaperValidationService,
)
from research.services.edge_discovery_lab import EdgeDiscoveryConfig, EdgeDiscoveryLabService
from research.services.edge_external_validation_lab import (
    EdgeExternalValidationLabService,
    ExternalStrategyValidationConfig,
)
from research.services.edge_extraction_lab import EdgeExtractionConfig, EdgeExtractionLabService
from research.services.market_regime_router_phase18 import MarketRegimeRouterConfig, MarketRegimeRouterService
from strategies.factory import create_strategy
from validation.validator import ValidationCriteria as WalkForwardCriteria
from validation.validator import OptimizationValidator, default_validation_window


@dataclass(frozen=True)
class ApprovedContext:
    symbol: str
    timeframe: str
    trend_bucket: str | None = None
    vol_regime: str | None = None
    recommended_strategy: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class OperationalHypothesisContract:
    strategy_name: str
    strategy_version: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    regime: str
    approved_contexts: tuple[ApprovedContext, ...]
    approved_filters: tuple[str, ...]
    approved_parameters: dict[str, Any]
    promotion_criteria: dict[str, Any]
    hypothesis_status: str
    status_history: tuple[str, ...]


@dataclass(frozen=True)
class EdgeOperationalPipelineConfig:
    prioritized_strategies: tuple[str, ...] = (
        "Ichimoku Kumo Breakout",
        "ClassicDonchianBreakout",
        "ClassicATRBreakout",
    )
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    timeframes: tuple[str, ...] = ("5m", "15m", "1h")
    window_days: int = 180
    capital: float = 10_000.0
    max_bars: int = 4500
    min_trades_per_filter: int = 20
    top_filters: int = 6
    max_candidate_filters: int = 30
    min_external_candidates: int = 5
    max_external_candidates: int = 10
    enable_web_research: bool = True
    strict_web_filters: bool = False
    min_repo_stars: int = 25
    max_inactive_days: int = 180
    reject_forks: bool = True
    require_readme: bool = True
    strategy_version: str = "v1.0"
    default_platform_strategy: str = "ClassicDonchianBreakout"
    walk_forward_min_trades: int = 20
    walk_forward_min_profit_factor: float = 1.0
    walk_forward_max_drawdown_pct: float = 35.0
    walk_forward_min_win_rate_pct: float = 40.0
    walk_forward_min_expectancy: float = 0.0
    walk_forward_min_sharpe: float = 0.0
    run_specialized_validation_live: bool = False
    specialized_validation_max_global_cycles: int = 0
    specialized_campaign_max_cycles_per_context: int = 1
    output_prefix: str = "edge_operational_pipeline"


class EdgeOperationalPipelineService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: EdgeOperationalPipelineConfig) -> dict[str, Any]:
        phase_sequence: list[str] = []
        status_history: list[str] = ["DISCOVERY_PENDING"]

        discovery = self._run_edge_discovery(cfg)
        phase_sequence.append("EDGE_DISCOVERY")
        status_history.append("DISCOVERED")

        extraction = self._run_edge_extraction(cfg)
        phase_sequence.append("EDGE_EXTRACTION")
        status_history.append("EXTRACTED")

        extraction_outputs = extraction.get("outputs", {}) if isinstance(extraction.get("outputs"), dict) else {}
        extraction_json = str(extraction_outputs.get("json") or "")
        if not extraction_json or not Path(extraction_json).exists():
            raise RuntimeError("EDGE extraction did not produce JSON artifact required for external validation.")

        external_validation = self._run_external_validation(cfg, extraction_json)
        phase_sequence.append("EDGE_EXTERNAL_VALIDATION")
        status_history.append("EXTERNALLY_VALIDATED")

        router = self._run_market_router(cfg)
        phase_sequence.append("MARKET_REGIME_ROUTER")
        status_history.append("ROUTED")

        contract = self._build_contract(cfg, discovery, extraction, router, status_history)

        backtest_result = self._run_backtest(cfg, contract)
        phase_sequence.append("BACKTEST")
        status_history.append("BACKTEST_COMPLETED" if bool(backtest_result.get("ok")) else "BACKTEST_FAILED")

        walk_forward_result = self._run_walk_forward(cfg, contract)
        phase_sequence.append("WALK_FORWARD")
        wf_passed = bool(walk_forward_result.get("passed"))
        status_history.append("WALK_FORWARD_APPROVED" if wf_passed else "WALK_FORWARD_REJECTED")

        approved_parameters = walk_forward_result.get("best_parameters") if isinstance(walk_forward_result.get("best_parameters"), dict) else {}
        if approved_parameters:
            contract = OperationalHypothesisContract(
                strategy_name=contract.strategy_name,
                strategy_version=contract.strategy_version,
                symbols=contract.symbols,
                timeframes=contract.timeframes,
                regime=contract.regime,
                approved_contexts=contract.approved_contexts,
                approved_filters=contract.approved_filters,
                approved_parameters=approved_parameters,
                promotion_criteria=contract.promotion_criteria,
                hypothesis_status="WALK_FORWARD_APPROVED" if wf_passed else "WALK_FORWARD_REJECTED",
                status_history=tuple(status_history),
            )

        specialized_validation = self._run_specialized_validation(cfg, contract, walk_forward_result)
        phase_sequence.append("PAPER_SPECIALIZED_VALIDATION")
        validation_answer = str((specialized_validation.get("summary", {}) or {}).get("verdict") or "")
        status_history.append("PAPER_SPECIALIZED_VALIDATED" if validation_answer == "SIM" else "PAPER_SPECIALIZED_CANDIDATE")

        specialized_campaign = self._run_specialized_campaign(cfg, contract, specialized_validation)
        phase_sequence.append("PAPER_SPECIALIZED_CAMPAIGN")
        campaign_answer = str((specialized_campaign.get("summary", {}) or {}).get("answer") or "")
        final_status = "PAPER_SPECIALIZED_CAMPAIGN_COMPLETED" if campaign_answer == "SIM" else "PAPER_SPECIALIZED_CAMPAIGN_RUNNING"
        status_history.append(final_status)

        final_contract = OperationalHypothesisContract(
            strategy_name=contract.strategy_name,
            strategy_version=contract.strategy_version,
            symbols=contract.symbols,
            timeframes=contract.timeframes,
            regime=contract.regime,
            approved_contexts=contract.approved_contexts,
            approved_filters=contract.approved_filters,
            approved_parameters=contract.approved_parameters,
            promotion_criteria=contract.promotion_criteria,
            hypothesis_status=final_status,
            status_history=tuple(status_history),
        )

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase_sequence": phase_sequence,
            "operational_hypothesis_contract": asdict(final_contract),
            "edge_discovery": {
                "summary": discovery.get("summary", {}),
                "outputs": discovery.get("outputs", {}),
            },
            "edge_extraction": {
                "summary": extraction.get("summary", {}),
                "outputs": extraction.get("outputs", {}),
            },
            "edge_external_validation": {
                "summary": external_validation.get("summary", {}),
                "outputs": external_validation.get("outputs", {}),
            },
            "market_regime_router": {
                "summary": router.get("summary", {}),
                "outputs": router.get("outputs", {}),
            },
            "backtest": backtest_result,
            "walk_forward": walk_forward_result,
            "paper_specialized_validation": {
                "summary": specialized_validation.get("summary", {}),
                "outputs": specialized_validation.get("outputs", {}),
            },
            "paper_specialized_campaign": {
                "summary": specialized_campaign.get("summary", {}),
                "outputs": specialized_campaign.get("outputs", {}),
            },
            "final_executive_report": {
                "winning_strategy": {
                    "strategy": final_contract.strategy_name,
                    "version": final_contract.strategy_version,
                },
                "scientific_result": {
                    "has_operational_edge": "SIM" if campaign_answer == "SIM" else "PARCIALMENTE",
                    "hypothesis_status": final_contract.hypothesis_status,
                    "status_history": list(final_contract.status_history),
                },
                "recommendation": "Proceed to controlled LIVE promotion gate." if campaign_answer == "SIM" else "Keep specialized campaign running until criteria are met.",
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        summary = {
            "pipeline": "EDGE_DISCOVERY -> EDGE_EXTRACTION -> EDGE_EXTERNAL_VALIDATION -> MARKET_REGIME_ROUTER -> BACKTEST -> WALK_FORWARD -> PAPER_SPECIALIZED_VALIDATION -> PAPER_SPECIALIZED_CAMPAIGN",
            "strategy": final_contract.strategy_name,
            "strategy_version": final_contract.strategy_version,
            "hypothesis_status": final_contract.hypothesis_status,
            "walk_forward_passed": wf_passed,
            "paper_validation_answer": validation_answer,
            "paper_campaign_answer": campaign_answer,
        }
        return {
            "summary": summary,
            "contract": asdict(final_contract),
            "report": report,
            "outputs": outputs,
        }

    def _run_edge_discovery(self, cfg: EdgeOperationalPipelineConfig) -> dict[str, Any]:
        service = EdgeDiscoveryLabService(self._base_dir)
        return service.run(
            EdgeDiscoveryConfig(
                symbols=cfg.symbols,
                timeframes=cfg.timeframes,
                window_days=max(10, int(cfg.window_days)),
                capital=max(100.0, float(cfg.capital)),
                max_bars=max(500, int(cfg.max_bars)),
                min_trades_per_context=max(3, int(cfg.min_trades_per_filter)),
                output_prefix="edge_discovery_lab",
            )
        )

    def _run_edge_extraction(self, cfg: EdgeOperationalPipelineConfig) -> dict[str, Any]:
        service = EdgeExtractionLabService(self._base_dir)
        return service.run(
            EdgeExtractionConfig(
                prioritized_strategies=cfg.prioritized_strategies,
                symbols=cfg.symbols,
                timeframes=cfg.timeframes,
                window_days=max(10, int(cfg.window_days)),
                capital=max(100.0, float(cfg.capital)),
                max_bars=max(500, int(cfg.max_bars)),
                min_trades_per_filter=max(5, int(cfg.min_trades_per_filter)),
                top_filters=max(1, int(cfg.top_filters)),
                max_candidate_filters=max(5, int(cfg.max_candidate_filters)),
                output_prefix="edge_extraction_lab",
            )
        )

    def _run_external_validation(self, cfg: EdgeOperationalPipelineConfig, extraction_json: str) -> dict[str, Any]:
        service = EdgeExternalValidationLabService(self._base_dir)
        return service.run(
            ExternalStrategyValidationConfig(
                edge01_report_file=extraction_json,
                min_external_candidates=max(1, int(cfg.min_external_candidates)),
                max_external_candidates=max(1, int(cfg.max_external_candidates)),
                enable_web_research=bool(cfg.enable_web_research),
                strict_web_filters=bool(cfg.strict_web_filters),
                min_repo_stars=max(0, int(cfg.min_repo_stars)),
                max_inactive_days=max(1, int(cfg.max_inactive_days)),
                reject_forks=bool(cfg.reject_forks),
                require_readme=bool(cfg.require_readme),
                output_prefix="edge_external_validation_lab",
            )
        )

    def _run_market_router(self, cfg: EdgeOperationalPipelineConfig) -> dict[str, Any]:
        service = MarketRegimeRouterService(self._base_dir)
        return service.run(
            MarketRegimeRouterConfig(
                symbols=cfg.symbols,
                timeframes=cfg.timeframes,
                window_days=max(10, int(cfg.window_days)),
                capital=max(100.0, float(cfg.capital)),
                max_bars=max(500, int(cfg.max_bars)),
                min_trades_per_regime=max(3, int(cfg.min_trades_per_filter)),
                output_prefix="phase18_market_regime_router",
            )
        )

    def _build_contract(
        self,
        cfg: EdgeOperationalPipelineConfig,
        discovery: dict[str, Any],
        extraction: dict[str, Any],
        router: dict[str, Any],
        status_history: list[str],
    ) -> OperationalHypothesisContract:
        router_map = ((router.get("report", {}) or {}).get("router_map", [])) if isinstance(router.get("report"), dict) else []
        contexts: list[ApprovedContext] = []
        seen_ctx: set[tuple[str, str]] = set()
        for row in router_map:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            if not symbol or not timeframe:
                continue
            key = (symbol, timeframe)
            if key in seen_ctx:
                continue
            seen_ctx.add(key)
            contexts.append(
                ApprovedContext(
                    symbol=symbol,
                    timeframe=timeframe,
                    trend_bucket=str(row.get("trend_bucket") or "") or None,
                    vol_regime=str(row.get("vol_regime") or "") or None,
                    recommended_strategy=str(row.get("recommended_platform_strategy") or row.get("recommended_strategy") or "") or None,
                    score=float(row.get("score")) if row.get("score") is not None else None,
                )
            )

        extraction_report = extraction.get("report", {}) if isinstance(extraction.get("report"), dict) else {}
        candidate_filters = extraction_report.get("candidate_filters", []) if isinstance(extraction_report.get("candidate_filters"), list) else []
        approved_filters = tuple(
            [str(item.get("rule")) for item in candidate_filters if isinstance(item, dict) and str(item.get("rule") or "").strip()]
        )

        strategy_name = self._choose_strategy_name(cfg, discovery, extraction, router, contexts)
        regime = self._choose_regime(router)

        promotion_criteria = {
            "walk_forward": {
                "min_trades": max(1, int(cfg.walk_forward_min_trades)),
                "min_profit_factor": float(cfg.walk_forward_min_profit_factor),
                "max_drawdown_pct": float(cfg.walk_forward_max_drawdown_pct),
                "min_win_rate_pct": float(cfg.walk_forward_min_win_rate_pct),
                "min_expectancy": float(cfg.walk_forward_min_expectancy),
                "min_sharpe": float(cfg.walk_forward_min_sharpe),
            },
            "paper_specialized_validation": {
                "final_answer_required": "SIM",
                "status_required": "PAPER_APPROVED_SPECIALIZED",
            },
            "paper_specialized_campaign": {
                "final_answer_required": "SIM",
                "final_status_required": "PAPER_APPROVED_SPECIALIZED",
            },
        }

        return OperationalHypothesisContract(
            strategy_name=strategy_name,
            strategy_version=str(cfg.strategy_version),
            symbols=tuple(cfg.symbols),
            timeframes=tuple(cfg.timeframes),
            regime=regime,
            approved_contexts=tuple(contexts),
            approved_filters=approved_filters,
            approved_parameters={},
            promotion_criteria=promotion_criteria,
            hypothesis_status="ROUTED",
            status_history=tuple(status_history),
        )

    def _choose_strategy_name(
        self,
        cfg: EdgeOperationalPipelineConfig,
        discovery: dict[str, Any],
        extraction: dict[str, Any],
        router: dict[str, Any],
        contexts: list[ApprovedContext],
    ) -> str:
        for ctx in contexts:
            if ctx.recommended_strategy:
                return str(ctx.recommended_strategy)

        discovery_decision = ((discovery.get("report", {}) or {}).get("decision", {})) if isinstance(discovery.get("report"), dict) else {}
        ranking = discovery_decision.get("ranking", []) if isinstance(discovery_decision.get("ranking"), list) else []
        if ranking:
            top_name = str((ranking[0] or {}).get("strategy") or "").strip()
            if top_name:
                return top_name

        extraction_summary = extraction.get("summary", {}) if isinstance(extraction.get("summary"), dict) else {}
        selected = extraction_summary.get("selected_strategies", []) if isinstance(extraction_summary.get("selected_strategies"), list) else []
        if selected:
            candidate = str(selected[0] or "").strip()
            if candidate:
                return candidate

        router_summary = router.get("summary", {}) if isinstance(router.get("summary"), dict) else {}
        baseline_name = str(router_summary.get("baseline_single_strategy") or "").strip()
        if baseline_name:
            return baseline_name

        return str(cfg.default_platform_strategy)

    def _choose_regime(self, router: dict[str, Any]) -> str:
        summary = router.get("summary", {}) if isinstance(router.get("summary"), dict) else {}
        conclusion = str(summary.get("conclusion") or "").strip()
        if conclusion:
            return conclusion
        return "dynamic_market_regime_router"

    def _load_market_data(self, symbol: str, timeframe: str, window_days: int, max_bars: int) -> pd.DataFrame:
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(10, int(window_days)))
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start_dt, end_dt)

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

    def _run_backtest(self, cfg: EdgeOperationalPipelineConfig, contract: OperationalHypothesisContract) -> dict[str, Any]:
        symbol = contract.symbols[0] if contract.symbols else ""
        timeframe = contract.timeframes[0] if contract.timeframes else ""
        if not symbol or not timeframe:
            return {"ok": False, "reason": "missing_symbol_or_timeframe"}

        data = self._load_market_data(symbol, timeframe, cfg.window_days, cfg.max_bars)
        if data.empty:
            return {"ok": False, "reason": "no_market_data", "symbol": symbol, "timeframe": timeframe}

        try:
            strategy = create_strategy(contract.strategy_name, **contract.approved_parameters)
            strategy = wrap_strategy_with_hypothesis(
                strategy,
                self._gate_config_from_contract(contract),
                symbol=symbol,
                timeframe=timeframe,
            )
            strategy.initialize()
            strategy.prepare_dataset(data.copy(), symbol=symbol, timeframe=None)
            engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=max(100.0, float(cfg.capital))))
            result = engine.run(data, symbol=symbol, timeframe=timeframe)
            return {
                "ok": True,
                "symbol": symbol,
                "timeframe": timeframe,
                "metrics": result.metrics.to_dict(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "reason": "backtest_error",
                "error": str(exc),
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy": contract.strategy_name,
            }

    def _run_walk_forward(self, cfg: EdgeOperationalPipelineConfig, contract: OperationalHypothesisContract) -> dict[str, Any]:
        symbol = contract.symbols[0] if contract.symbols else ""
        timeframe = contract.timeframes[0] if contract.timeframes else ""
        if not symbol or not timeframe:
            return {"passed": False, "reason": "missing_symbol_or_timeframe"}

        window = default_validation_window(start=None, end=None, symbol=symbol, timeframe=timeframe)

        gate_config = self._gate_config_from_contract(contract)

        def _strategy_factory(parameters: dict[str, float | int], run_symbol: str, run_timeframe: str) -> Any:
            strategy = create_strategy(contract.strategy_name, **parameters)
            strategy = wrap_strategy_with_hypothesis(
                strategy,
                gate_config,
                symbol=run_symbol,
                timeframe=run_timeframe,
            )
            strategy.initialize()
            return strategy

        validator = OptimizationValidator(
            criteria=WalkForwardCriteria(
                min_trades=max(1, int(cfg.walk_forward_min_trades)),
                min_profit_factor=float(cfg.walk_forward_min_profit_factor),
                max_drawdown_pct=float(cfg.walk_forward_max_drawdown_pct),
                min_win_rate_pct=float(cfg.walk_forward_min_win_rate_pct),
                min_expectancy=float(cfg.walk_forward_min_expectancy),
                min_sharpe=float(cfg.walk_forward_min_sharpe),
            ),
            output_dir=self._results_dir,
            strategy_name=contract.strategy_name,
            strategy_factory=_strategy_factory,
        )

        candidate = OptimizationResult(
            rank=1,
            parameters=dict(contract.approved_parameters or {}),
            metrics={},
            combinations_tested=1,
            runtime_seconds=0.0,
        )

        try:
            summary = validator.validate(
                optimization_results=[candidate],
                symbol=symbol,
                timeframe=timeframe,
                capital=max(100.0, float(cfg.capital)),
                train_start=window.train_start,
                train_end=window.train_end,
                validation_start=window.validation_start,
                validation_end=window.validation_end,
                top_n=1,
            )
        except Exception as exc:
            return {
                "passed": False,
                "reason": "walk_forward_error",
                "error": str(exc),
                "symbol": symbol,
                "timeframe": timeframe,
            }

        best = summary.best_validated
        passed = summary.passed > 0 and best is not None and bool(best.passed)
        best_parameters = best.parameters if best is not None and isinstance(best.parameters, dict) else {}
        validation_metrics = best.validation_metrics if best is not None and isinstance(best.validation_metrics, dict) else {}
        train_metrics = best.train_metrics if best is not None and isinstance(best.train_metrics, dict) else {}

        return {
            "passed": bool(passed),
            "total_candidates": int(summary.total_candidates),
            "passed_candidates": int(summary.passed),
            "discarded_candidates": int(summary.discarded),
            "best_parameters": best_parameters,
            "best_train_metrics": train_metrics,
            "best_validation_metrics": validation_metrics,
            "output_files": list(summary.output_files),
            "symbol": symbol,
            "timeframe": timeframe,
        }

    def _build_specialized_contexts(self, contract: OperationalHypothesisContract) -> list[SpecializedContext]:
        contexts: list[SpecializedContext] = []
        seen: set[tuple[str, str]] = set()
        for row in contract.approved_contexts:
            key = (row.symbol, row.timeframe)
            if key in seen:
                continue
            seen.add(key)
            contexts.append(SpecializedContext(symbol=row.symbol, timeframe=row.timeframe))
        if not contexts and contract.symbols and contract.timeframes:
            contexts.append(SpecializedContext(symbol=contract.symbols[0], timeframe=contract.timeframes[0]))
        return contexts

    def _baseline_from_walk_forward(self, walk_forward_result: dict[str, Any]) -> SpecializedBaseline:
        metrics = walk_forward_result.get("best_validation_metrics", {}) if isinstance(walk_forward_result.get("best_validation_metrics"), dict) else {}
        drawdown = metrics.get("max_drawdown_pct")
        drawdown_value = abs(float(drawdown)) if drawdown is not None else None
        return SpecializedBaseline(
            profit_factor=float(metrics.get("profit_factor")) if metrics.get("profit_factor") is not None else None,
            sharpe=float(metrics.get("sharpe_ratio")) if metrics.get("sharpe_ratio") is not None else None,
            expectancy=float(metrics.get("expectancy")) if metrics.get("expectancy") is not None else None,
            drawdown=drawdown_value,
            win_rate=float(metrics.get("win_rate")) if metrics.get("win_rate") is not None else None,
        )

    def _run_specialized_validation(
        self,
        cfg: EdgeOperationalPipelineConfig,
        contract: OperationalHypothesisContract,
        walk_forward_result: dict[str, Any],
    ) -> dict[str, Any]:
        service = SpecializedPaperValidationService(self._base_dir)
        contexts = tuple(self._build_specialized_contexts(contract))
        baseline = self._baseline_from_walk_forward(walk_forward_result)
        return service.run(
            SpecializedPaperValidationConfig(
                strategy_name=contract.strategy_name,
                strategy_version=contract.strategy_version,
                run_live=bool(cfg.run_specialized_validation_live),
                max_global_cycles=max(0, int(cfg.specialized_validation_max_global_cycles)),
                initial_capital=max(100.0, float(cfg.capital)),
                contexts=contexts,
                contexts_from_matrix=False,
                hypothesis_config=self._hypothesis_payload(contract),
                backtest_baseline=baseline,
                rolling_oos_baseline=baseline,
                output_prefix="paper_specialized_validation",
            )
        )

    def _run_specialized_campaign(
        self,
        cfg: EdgeOperationalPipelineConfig,
        contract: OperationalHypothesisContract,
        specialized_validation: dict[str, Any],
    ) -> dict[str, Any]:
        service = SpecializedPaperCampaignService(self._base_dir)
        contexts = tuple([EdgeDriftContext(symbol=row.symbol, timeframe=row.timeframe) for row in self._build_specialized_contexts(contract)])
        outputs = specialized_validation.get("outputs", {}) if isinstance(specialized_validation.get("outputs"), dict) else {}
        specialized_report_file = str(outputs.get("json") or "") or None
        return service.run(
            SpecializedCampaignConfig(
                strategy_name=contract.strategy_name,
                strategy_version=contract.strategy_version,
                specialized_report_file=specialized_report_file,
                contexts=contexts,
                contexts_from_latest_report=False,
                hypothesis_config=self._hypothesis_payload(contract),
                max_cycles_per_context=max(1, int(cfg.specialized_campaign_max_cycles_per_context)),
                initial_capital=max(100.0, float(cfg.capital)),
                output_prefix="paper_specialized_campaign",
            )
        )

    def _hypothesis_payload(self, contract: OperationalHypothesisContract) -> dict[str, Any]:
        return {
            "approved_parameters": dict(contract.approved_parameters or {}),
            "approved_filters": list(contract.approved_filters),
            "regime": contract.regime,
            "approved_contexts": [asdict(ctx) for ctx in contract.approved_contexts],
        }

    def _gate_config_from_contract(self, contract: OperationalHypothesisContract) -> HypothesisGateConfig:
        return hypothesis_gate_config_from_payload(self._hypothesis_payload(contract))

    def _write_outputs(self, output_prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")

        return {
            "json": str(json_path),
            "md": str(md_path),
        }

    def _to_markdown(self, report: dict[str, Any]) -> str:
        final = report.get("final_executive_report", {}) if isinstance(report.get("final_executive_report"), dict) else {}
        winning = final.get("winning_strategy") if isinstance(final.get("winning_strategy"), dict) else {}
        science = final.get("scientific_result") if isinstance(final.get("scientific_result"), dict) else {}
        contract = report.get("operational_hypothesis_contract", {}) if isinstance(report.get("operational_hypothesis_contract"), dict) else {}
        phase_sequence = report.get("phase_sequence", []) if isinstance(report.get("phase_sequence"), list) else []

        lines = [
            "# EDGE Operational Pipeline",
            "",
            "## Sequence",
            f"- {' -> '.join([str(item) for item in phase_sequence])}",
            "- Full chain executed automatically without manual intervention",
            "",
            "## Operational Hypothesis Contract",
            f"- Strategy: {contract.get('strategy_name')}",
            f"- Version: {contract.get('strategy_version')}",
            f"- Regime: {contract.get('regime')}",
            f"- Status: {contract.get('hypothesis_status')}",
            f"- Approved contexts: {len(contract.get('approved_contexts') or [])}",
            f"- Approved filters: {len(contract.get('approved_filters') or [])}",
            "",
            "## Winning Strategy",
            f"- Name: {winning.get('strategy') if winning else 'none'}",
            f"- Version: {winning.get('version') if winning else 'none'}",
            "",
            "## Scientific Result",
            f"- Has operational edge for prolonged paper? {science.get('has_operational_edge', 'PARCIALMENTE')}",
            f"- Hypothesis status: {science.get('hypothesis_status')}",
            f"- Recommendation: {final.get('recommendation')}",
        ]
        return "\n".join(lines) + "\n"
