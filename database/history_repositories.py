"""Repositories for execution history and analytics persistence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from database.history_models import (
    BacktestRun,
    ExecutionFrameworkOptimizationRun,
    IndicatorHistorySnapshot,
    OptimizationRun,
    OptimizationResultRecord,
    ScientificReadinessHistory,
    ScientificTradeSnapshot,
    ScientificRobustnessValidationRun,
    StrategyFamilyCatalog,
    StrategyFamilyDiscoveryRun,
    SignalSnapshot,
    TradeOutcomeImplementationRun,
    TradeOutcomeLearningRun,
    TradeHistory,
    ValidationRun,
)
from database.session_models import ExecutionSession, StrategyVersion
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CheckpointState:
    execution_id: str
    processed: int
    completed: bool
    last_rank: int | None = None
    updated_at: datetime | None = None


class HistoryRepositoryBase:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _commit_flush(self) -> None:
        self._session.flush()
        self._session.commit()


class OptimizationRunRepository(HistoryRepositoryBase):
    def create(self, run: OptimizationRun) -> OptimizationRun:
        existing = self._session.execute(
            select(OptimizationRun).where(OptimizationRun.execution_id == run.execution_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.started_at = run.started_at
            existing.strategy = run.strategy
            existing.symbol = run.symbol
            existing.timeframe = run.timeframe
            existing.total_combinations = run.total_combinations
            existing.workers = run.workers
            existing.status = run.status
            self._commit_flush()
            logger.info("Optimization run reused: execution_id=%s", run.execution_id)
            return existing
        self._session.add(run)
        self._commit_flush()
        logger.info("Optimization run saved: execution_id=%s", run.execution_id)
        return run

    def update_status(
        self,
        execution_id: str,
        status: str,
        finished_at: datetime | None = None,
        duration_seconds: float | None = None,
    ) -> OptimizationRun | None:
        run = self._session.execute(
            select(OptimizationRun).where(OptimizationRun.execution_id == execution_id)
        ).scalar_one_or_none()
        if run is None:
            return None
        run.status = status
        run.finished_at = finished_at
        run.duration_seconds = duration_seconds
        self._commit_flush()
        logger.info("Optimization run updated: execution_id=%s status=%s", execution_id, status)
        return run


class OptimizationResultRepository(HistoryRepositoryBase):
    @staticmethod
    def _sanitize_float(value: float | None) -> float | None:
        if value is None:
            return None
        return value if math.isfinite(value) else None

    def save(self, result: OptimizationResultRecord) -> OptimizationResultRecord:
        # MySQL rejects inf/-inf/NaN values; normalize unstable metrics to NULL.
        result.win_rate = self._sanitize_float(result.win_rate)
        result.profit_factor = self._sanitize_float(result.profit_factor)
        result.net_profit = self._sanitize_float(result.net_profit)
        result.return_percent = self._sanitize_float(result.return_percent)
        result.drawdown = self._sanitize_float(result.drawdown)
        result.sharpe = self._sanitize_float(result.sharpe)
        result.expectancy = self._sanitize_float(result.expectancy)
        self._session.add(result)
        self._commit_flush()
        logger.info(
            "Optimization result saved: execution_id=%s approved=%s profit_factor=%s",
            result.execution_id,
            result.approved,
            result.profit_factor,
        )
        return result

    def count_by_execution(self, execution_id: str) -> int:
        return self._session.scalar(
            select(func.count()).select_from(OptimizationResultRecord).where(
                OptimizationResultRecord.execution_id == execution_id
            )
        ) or 0

    def get_best_profit_factor(self) -> OptimizationResultRecord | None:
        return self._session.execute(
            select(OptimizationResultRecord)
            .where(OptimizationResultRecord.approved.is_(True))
            .order_by(desc(OptimizationResultRecord.profit_factor), desc(OptimizationResultRecord.net_profit))
        ).scalars().first()

    def get_best_ema_fast(self) -> list[tuple[int | None, float | None]]:
        rows = self._session.execute(
            select(OptimizationResultRecord.ema_fast, func.max(OptimizationResultRecord.net_profit))
            .where(OptimizationResultRecord.approved.is_(True))
            .group_by(OptimizationResultRecord.ema_fast)
            .order_by(desc(func.max(OptimizationResultRecord.net_profit)))
        ).all()
        return [(row[0], row[1]) for row in rows]

    def get_most_common_rr_in_top(self, limit: int = 50) -> list[tuple[float | None, int]]:
        rows = self._session.execute(
            select(OptimizationResultRecord.risk_reward_ratio, func.count())
            .where(OptimizationResultRecord.approved.is_(True))
            .group_by(OptimizationResultRecord.risk_reward_ratio)
            .order_by(desc(func.count()))
            .limit(limit)
        ).all()
        return [(row[0], row[1]) for row in rows]

    def get_best_timeframe(self) -> list[tuple[str, float | None]]:
        rows = self._session.execute(
            select(OptimizationResultRecord.timeframe, func.max(OptimizationResultRecord.net_profit))
            .where(OptimizationResultRecord.approved.is_(True))
            .group_by(OptimizationResultRecord.timeframe)
            .order_by(desc(func.max(OptimizationResultRecord.net_profit)))
        ).all()
        return [(row[0], row[1]) for row in rows]

    def get_best_symbol(self) -> list[tuple[str, float | None]]:
        rows = self._session.execute(
            select(OptimizationResultRecord.symbol, func.max(OptimizationResultRecord.net_profit))
            .where(OptimizationResultRecord.approved.is_(True))
            .group_by(OptimizationResultRecord.symbol)
            .order_by(desc(func.max(OptimizationResultRecord.net_profit)))
        ).all()
        return [(row[0], row[1]) for row in rows]

    def get_most_approved_configs(self) -> list[tuple[str, int]]:
        rows = self._session.execute(
            select(OptimizationResultRecord.parameters_json, func.count())
            .where(OptimizationResultRecord.approved.is_(True))
            .group_by(OptimizationResultRecord.parameters_json)
            .order_by(desc(func.count()))
        ).all()
        return [(row[0], row[1]) for row in rows]

    def get_lowest_drawdown(self) -> OptimizationResultRecord | None:
        return self._session.execute(
            select(OptimizationResultRecord)
            .where(OptimizationResultRecord.approved.is_(True))
            .order_by(OptimizationResultRecord.drawdown.asc())
        ).scalars().first()


class StrategyFamilyCatalogRepository(HistoryRepositoryBase):
    def upsert(self, entry: StrategyFamilyCatalog) -> StrategyFamilyCatalog:
        existing = self._session.execute(
            select(StrategyFamilyCatalog).where(StrategyFamilyCatalog.family == entry.family)
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(entry)
            self._commit_flush()
            logger.info("Strategy family catalog saved: family=%s status=%s", entry.family, entry.status)
            return entry

        for field_name in (
            "strategy_name",
            "strategy_version",
            "rank_position",
            "status",
            "validation_status",
            "research_status",
            "trade_management_status",
            "best_profit_factor",
            "best_sharpe",
            "best_expectancy",
            "discovery_score",
            "market_fit_score",
            "evidence_score",
            "tested_count",
            "approved_count",
            "pilot_symbol",
            "pilot_timeframe",
            "pilot_combinations",
            "pilot_workers",
            "reason",
            "selected_at",
        ):
            setattr(existing, field_name, getattr(entry, field_name))
        self._commit_flush()
        logger.info("Strategy family catalog updated: family=%s status=%s", entry.family, entry.status)
        return existing

    def list_all(self) -> list[StrategyFamilyCatalog]:
        return self._session.execute(
            select(StrategyFamilyCatalog).order_by(
                StrategyFamilyCatalog.rank_position.is_(None),
                StrategyFamilyCatalog.rank_position.asc(),
                StrategyFamilyCatalog.family.asc(),
            )
        ).scalars().all()


class StrategyFamilyDiscoveryRunRepository(HistoryRepositoryBase):
    def save(self, run: StrategyFamilyDiscoveryRun) -> StrategyFamilyDiscoveryRun:
        existing = self._session.execute(
            select(StrategyFamilyDiscoveryRun).where(StrategyFamilyDiscoveryRun.run_id == run.run_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.recommended_family = run.recommended_family
            existing.recommended_strategy = run.recommended_strategy
            existing.recommended_version = run.recommended_version
            existing.symbol = run.symbol
            existing.timeframe = run.timeframe
            existing.pilot_combinations = run.pilot_combinations
            existing.pilot_workers = run.pilot_workers
            existing.weights_json = run.weights_json
            existing.ranking_json = run.ranking_json
            existing.summary_json = run.summary_json
            self._commit_flush()
            logger.info("Strategy family discovery run reused: run_id=%s", run.run_id)
            return existing
        self._session.add(run)
        self._commit_flush()
        logger.info("Strategy family discovery run saved: run_id=%s", run.run_id)
        return run


class BacktestRunRepository(HistoryRepositoryBase):
    def save(self, run: BacktestRun) -> BacktestRun:
        self._session.add(run)
        self._commit_flush()
        logger.info("Backtest run saved: execution_id=%s", run.execution_id)
        return run


class TradeHistoryRepository(HistoryRepositoryBase):
    def save_many(self, trades: list[TradeHistory]) -> int:
        self._session.add_all(trades)
        self._commit_flush()
        logger.info("Trade history saved: rows=%d", len(trades))
        return len(trades)


class SignalHistoryRepository(HistoryRepositoryBase):
    def save_many(self, signals: list[SignalSnapshot]) -> int:
        self._session.add_all(signals)
        self._commit_flush()
        logger.info("Signals saved: rows=%d", len(signals))
        return len(signals)


class IndicatorSnapshotRepository(HistoryRepositoryBase):
    def save_many(self, snapshots: list[IndicatorHistorySnapshot]) -> int:
        self._session.add_all(snapshots)
        self._commit_flush()
        logger.info("Indicator snapshots saved: rows=%d", len(snapshots))
        return len(snapshots)


class ScientificTradeSnapshotRepository(HistoryRepositoryBase):
    def save(self, snapshot: ScientificTradeSnapshot) -> ScientificTradeSnapshot:
        self._session.add(snapshot)
        self._commit_flush()
        logger.info("Scientific trade snapshot saved: id=%s execution_id=%s", snapshot.id, snapshot.execution_id)
        return snapshot


class ScientificReadinessHistoryRepository(HistoryRepositoryBase):
    def save(self, record: ScientificReadinessHistory) -> ScientificReadinessHistory:
        self._session.add(record)
        self._commit_flush()
        logger.info("Scientific readiness history saved: id=%s strategy_key=%s", record.id, record.strategy_key)
        return record


class ValidationRunRepository(HistoryRepositoryBase):
    def save(self, run: ValidationRun) -> ValidationRun:
        self._session.add(run)
        self._commit_flush()
        logger.info("Validation run saved: execution_id=%s", run.execution_id)
        return run


class ScientificRobustnessValidationRunRepository(HistoryRepositoryBase):
    def save(self, run: ScientificRobustnessValidationRun) -> ScientificRobustnessValidationRun:
        existing = self._session.execute(
            select(ScientificRobustnessValidationRun).where(ScientificRobustnessValidationRun.run_id == run.run_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = run.status
            existing.decision = run.decision
            existing.approved = run.approved
            existing.candidate_cluster_id = run.candidate_cluster_id
            existing.candidate_rule = run.candidate_rule
            existing.scientific_robustness_score = run.scientific_robustness_score
            existing.operational_edge_score = run.operational_edge_score
            existing.temporal_robustness = run.temporal_robustness
            existing.asset_robustness = run.asset_robustness
            existing.regime_robustness = run.regime_robustness
            existing.generalization_score = run.generalization_score
            existing.statistical_stability = run.statistical_stability
            existing.rejection_reason = run.rejection_reason
            existing.artifacts_json = run.artifacts_json
            existing.summary_json = run.summary_json
            self._commit_flush()
            logger.info("Scientific robustness run updated: run_id=%s", run.run_id)
            return existing

        self._session.add(run)
        self._commit_flush()
        logger.info("Scientific robustness run saved: run_id=%s", run.run_id)
        return run


class TradeOutcomeLearningRunRepository(HistoryRepositoryBase):
    def save(self, run: TradeOutcomeLearningRun) -> TradeOutcomeLearningRun:
        existing = self._session.execute(
            select(TradeOutcomeLearningRun).where(TradeOutcomeLearningRun.run_id == run.run_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = run.status
            existing.decision = run.decision
            existing.approved = run.approved
            existing.target_name = run.target_name
            existing.rule_text = run.rule_text
            existing.trade_outcome_score = run.trade_outcome_score
            existing.expected_profit_factor = run.expected_profit_factor
            existing.expected_expectancy = run.expected_expectancy
            existing.expected_sharpe = run.expected_sharpe
            existing.temporal_robustness = run.temporal_robustness
            existing.asset_robustness = run.asset_robustness
            existing.regime_robustness = run.regime_robustness
            existing.timeframe_robustness = run.timeframe_robustness
            existing.generalization_score = run.generalization_score
            existing.simplicity_score = run.simplicity_score
            existing.coverage_score = run.coverage_score
            existing.overfit_flag = run.overfit_flag
            existing.rejection_reason = run.rejection_reason
            existing.artifacts_json = run.artifacts_json
            existing.summary_json = run.summary_json
            self._commit_flush()
            logger.info("Trade outcome learning run updated: run_id=%s", run.run_id)
            return existing

        self._session.add(run)
        self._commit_flush()
        logger.info("Trade outcome learning run saved: run_id=%s", run.run_id)
        return run


class TradeOutcomeImplementationRunRepository(HistoryRepositoryBase):
    def save(self, run: TradeOutcomeImplementationRun) -> TradeOutcomeImplementationRun:
        existing = self._session.execute(
            select(TradeOutcomeImplementationRun).where(TradeOutcomeImplementationRun.run_id == run.run_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = run.status
            existing.decision = run.decision
            existing.strategy_name = run.strategy_name
            existing.target_name = run.target_name
            existing.rule_text = run.rule_text
            existing.fidelity_precision = run.fidelity_precision
            existing.fidelity_recall = run.fidelity_recall
            existing.fidelity_f1 = run.fidelity_f1
            existing.overlap_count = run.overlap_count
            existing.false_positives = run.false_positives
            existing.false_negatives = run.false_negatives
            existing.expected_profit_factor = run.expected_profit_factor
            existing.observed_profit_factor = run.observed_profit_factor
            existing.expected_sharpe = run.expected_sharpe
            existing.observed_sharpe = run.observed_sharpe
            existing.expected_expectancy = run.expected_expectancy
            existing.observed_expectancy = run.observed_expectancy
            existing.expected_drawdown = run.expected_drawdown
            existing.observed_drawdown = run.observed_drawdown
            existing.artifacts_json = run.artifacts_json
            existing.summary_json = run.summary_json
            self._commit_flush()
            logger.info("Trade outcome implementation run updated: run_id=%s", run.run_id)
            return existing

        self._session.add(run)
        self._commit_flush()
        logger.info("Trade outcome implementation run saved: run_id=%s", run.run_id)
        return run


class ExecutionFrameworkOptimizationRunRepository(HistoryRepositoryBase):
    def save(self, run: ExecutionFrameworkOptimizationRun) -> ExecutionFrameworkOptimizationRun:
        existing = self._session.execute(
            select(ExecutionFrameworkOptimizationRun).where(ExecutionFrameworkOptimizationRun.run_id == run.run_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = run.status
            existing.strategy_name = run.strategy_name
            existing.benchmark_symbol = run.benchmark_symbol
            existing.benchmark_timeframe = run.benchmark_timeframe
            existing.same_trades = run.same_trades
            existing.same_metrics = run.same_metrics
            existing.equivalence_passed = run.equivalence_passed
            existing.bars_before = run.bars_before
            existing.bars_after = run.bars_after
            existing.speedup_pct = run.speedup_pct
            existing.time_before_seconds = run.time_before_seconds
            existing.time_after_seconds = run.time_after_seconds
            existing.first_result_before_seconds = run.first_result_before_seconds
            existing.first_result_after_seconds = run.first_result_after_seconds
            existing.audit_json = run.audit_json
            existing.benchmark_json = run.benchmark_json
            existing.artifacts_json = run.artifacts_json
            existing.summary_json = run.summary_json
            self._commit_flush()
            logger.info("Execution framework optimization run updated: run_id=%s", run.run_id)
            return existing

        self._session.add(run)
        self._commit_flush()
        logger.info("Execution framework optimization run saved: run_id=%s", run.run_id)
        return run


class StrategyVersionRepository(HistoryRepositoryBase):
    def get_or_create(
        self,
        strategy_name: str,
        version: str,
        git_commit: str | None,
        description: str | None = None,
    ) -> StrategyVersion:
        existing = self._session.execute(
            select(StrategyVersion).where(
                StrategyVersion.strategy_name == strategy_name,
                StrategyVersion.version == version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if git_commit is not None:
                existing.git_commit = git_commit
            if description is not None:
                existing.description = description
            existing.active = True
            self._commit_flush()
            logger.info(
                "Strategy version reused: strategy=%s version=%s",
                strategy_name,
                version,
            )
            return existing

        record = StrategyVersion(
            strategy_name=strategy_name,
            version=version,
            git_commit=git_commit,
            description=description,
            active=True,
        )
        self._session.add(record)
        self._commit_flush()
        logger.info("Strategy version saved: strategy=%s version=%s", strategy_name, version)
        return record


class ExecutionSessionRepository(HistoryRepositoryBase):
    def create_or_update_started(
        self,
        execution_id: str,
        started_at: datetime,
        status: str,
        host: str | None,
        cpu: str | None,
        workers: int | None,
        python_version: str | None,
        git_version: str | None,
    ) -> ExecutionSession:
        existing = self._session.execute(
            select(ExecutionSession).where(ExecutionSession.execution_id == execution_id)
        ).scalar_one_or_none()
        if existing is None:
            existing = ExecutionSession(
                execution_id=execution_id,
                started_at=started_at,
                status=status,
                host=host,
                cpu=cpu,
                workers=workers,
                python_version=python_version,
                git_version=git_version,
            )
            self._session.add(existing)
        else:
            existing.started_at = started_at
            existing.status = status
            existing.host = host
            existing.cpu = cpu
            existing.workers = workers
            existing.python_version = python_version
            existing.git_version = git_version

        self._commit_flush()
        logger.info("Execution session started: execution_id=%s status=%s", execution_id, status)
        return existing

    def finish(
        self,
        execution_id: str,
        finished_at: datetime,
        duration: float,
        status: str,
    ) -> ExecutionSession | None:
        existing = self._session.execute(
            select(ExecutionSession).where(ExecutionSession.execution_id == execution_id)
        ).scalar_one_or_none()
        if existing is None:
            return None
        existing.finished_at = finished_at
        existing.duration = duration
        existing.status = status
        self._commit_flush()
        logger.info("Execution session finished: execution_id=%s status=%s", execution_id, status)
        return existing
