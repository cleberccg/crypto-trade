"""Persistent history tables for system executions and analytics."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models import Base


class StrategyFamilyCatalog(Base):
    __tablename__ = "strategy_family_catalog"
    __table_args__ = (
        UniqueConstraint("family", name="uq_strategy_family_catalog_family"),
        Index("ix_strategy_family_catalog_family", "family"),
        Index("ix_strategy_family_catalog_status", "status"),
        Index("ix_strategy_family_catalog_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rank_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(30), nullable=False)
    research_status: Mapped[str] = mapped_column(String(30), nullable=False)
    trade_management_status: Mapped[str] = mapped_column(String(30), nullable=False)
    best_profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    discovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pilot_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pilot_timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pilot_combinations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pilot_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyFamilyDiscoveryRun(Base):
    __tablename__ = "strategy_family_discovery_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_strategy_family_discovery_runs_run_id"),
        Index("ix_strategy_family_discovery_runs_run_id", "run_id"),
        Index("ix_strategy_family_discovery_runs_recommended_family", "recommended_family"),
        Index("ix_strategy_family_discovery_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recommended_family: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recommended_strategy: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recommended_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pilot_combinations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pilot_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    ranking_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"
    __table_args__ = (
        Index("ix_history_optimization_runs_execution_id", "execution_id"),
        Index("ix_history_optimization_runs_strategy", "strategy"),
        Index("ix_history_optimization_runs_symbol", "symbol"),
        Index("ix_history_optimization_runs_timeframe", "timeframe"),
        Index("ix_history_optimization_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    total_combinations: Mapped[int] = mapped_column(Integer, nullable=False)
    workers: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    results: Mapped[list[OptimizationResultRecord]] = relationship(
        "OptimizationResultRecord", back_populates="run", cascade="all, delete-orphan"
    )


class OptimizationResultRecord(Base):
    __tablename__ = "optimization_results_history"
    __table_args__ = (
        UniqueConstraint("execution_id", "parameters_json", name="uq_optimization_result_execution_params"),
        Index("ix_history_optimization_results_execution_id", "execution_id"),
        Index("ix_history_optimization_results_strategy", "strategy"),
        Index("ix_history_optimization_results_symbol", "symbol"),
        Index("ix_history_optimization_results_timeframe", "timeframe"),
        Index("ix_history_optimization_results_profit_factor", "profit_factor"),
        Index("ix_history_optimization_results_win_rate", "win_rate"),
        Index("ix_history_optimization_results_approved", "approved"),
        Index("ix_history_optimization_results_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("optimization_runs.id", ondelete="SET NULL"), nullable=True)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    ema_fast: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ema_slow: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ema_trend: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rsi_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_stop_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped[OptimizationRun | None] = relationship("OptimizationRun", back_populates="results")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_history_backtest_runs_execution_id", "execution_id"),
        Index("ix_history_backtest_runs_strategy", "strategy"),
        Index("ix_history_backtest_runs_symbol", "symbol"),
        Index("ix_history_backtest_runs_timeframe", "timeframe"),
        Index("ix_history_backtest_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    final_capital: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"
    __table_args__ = (
        Index("ix_history_signals_execution_id", "execution_id"),
        Index("ix_history_signals_strategy", "strategy"),
        Index("ix_history_signals_symbol", "symbol"),
        Index("ix_history_signals_timeframe", "timeframe"),
        Index("ix_history_signals_created_at", "created_at"),
        Index("ix_history_signals_approved", "accepted"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    indicator_snapshot: Mapped[IndicatorHistorySnapshot | None] = relationship(
        "IndicatorHistorySnapshot", back_populates="signal", cascade="all, delete-orphan", uselist=False
    )


class IndicatorHistorySnapshot(Base):
    __tablename__ = "indicator_snapshots"
    __table_args__ = (
        Index("ix_history_indicator_snapshots_signal_id", "signal_id"),
        Index("ix_history_indicator_snapshots_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signal_snapshots.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    ema_fast: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_slow: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_average: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    signal: Mapped[SignalSnapshot] = relationship("SignalSnapshot", back_populates="indicator_snapshot")


class TradeHistory(Base):
    __tablename__ = "trade_history"
    __table_args__ = (
        Index("ix_history_trade_history_execution_id", "execution_id"),
        Index("ix_history_trade_history_strategy", "strategy"),
        Index("ix_history_trade_history_symbol", "symbol"),
        Index("ix_history_trade_history_timeframe", "timeframe"),
        Index("ix_history_trade_history_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScientificTradeSnapshot(Base):
    __tablename__ = "scientific_trade_snapshots"
    __table_args__ = (
        Index("ix_scientific_trade_snapshots_execution_id", "execution_id"),
        Index("ix_scientific_trade_snapshots_strategy", "strategy"),
        Index("ix_scientific_trade_snapshots_symbol", "symbol"),
        Index("ix_scientific_trade_snapshots_timeframe", "timeframe"),
        Index("ix_scientific_trade_snapshots_entry_timestamp", "entry_timestamp"),
        Index("ix_scientific_trade_snapshots_trade_history_id", "trade_history_id"),
        Index("ix_scientific_trade_snapshots_campaign_id", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False, default="BUY")
    entry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal_snapshot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signal_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    trade_history_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("trade_history.id", ondelete="SET NULL"), nullable=True
    )
    entry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(120), nullable=True)
    trend_regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    volatility_regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    volume_regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    indicator_context_json: Mapped[str] = mapped_column(Text, nullable=False)
    entry_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    exit_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ScientificReadinessHistory(Base):
    __tablename__ = "scientific_readiness_history"
    __table_args__ = (
        Index("ix_scientific_readiness_history_generated_at", "generated_at"),
        Index("ix_scientific_readiness_history_strategy_key", "strategy_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(100), nullable=False)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    trades_by_asset_json: Mapped[str] = mapped_column(Text, nullable=False)
    trades_by_timeframe_json: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_days: Mapped[float] = mapped_column(Float, nullable=False)
    readiness_score: Mapped[float] = mapped_column(Float, nullable=False)
    readiness_label: Mapped[str] = mapped_column(String(40), nullable=False)
    criteria_hit_json: Mapped[str] = mapped_column(Text, nullable=False)
    criteria_pending_json: Mapped[str] = mapped_column(Text, nullable=False)
    outputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    __table_args__ = (
        Index("ix_history_validation_runs_execution_id", "execution_id"),
        Index("ix_history_validation_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    optimizer_run: Mapped[str | None] = mapped_column(String(36), nullable=True)
    total_tested: Mapped[int] = mapped_column(Integer, nullable=False)
    approved: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    min_profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExecutionCheckpoint(Base):
    __tablename__ = "execution_checkpoints"
    __table_args__ = (
        Index("ix_history_execution_checkpoints_execution_id", "execution_id"),
        Index("ix_history_execution_checkpoints_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    processed: Mapped[int] = mapped_column(Integer, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NotificationHistory(Base):
    __tablename__ = "notification_history"
    __table_args__ = (
        Index("ix_notification_history_created_at", "created_at"),
        Index("ix_notification_history_execution_id", "execution_id"),
        Index("ix_notification_history_type", "notification_type"),
        Index("ix_notification_history_status", "status"),
        Index("ix_notification_history_destination", "destination"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    destination: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="telegram")
    delivery_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScientificRobustnessValidationRun(Base):
    __tablename__ = "scientific_robustness_validation_runs"
    __table_args__ = (
        Index("ix_scientific_robustness_run_id", "run_id"),
        Index("ix_scientific_robustness_created_at", "created_at"),
        Index("ix_scientific_robustness_approved", "approved"),
        Index("ix_scientific_robustness_candidate_cluster", "candidate_cluster_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    decision: Mapped[str] = mapped_column(String(10), nullable=False, default="B")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    candidate_cluster_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    candidate_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    scientific_robustness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    operational_edge_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    temporal_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    asset_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    generalization_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    statistical_stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TradeOutcomeLearningRun(Base):
    __tablename__ = "trade_outcome_learning_runs"
    __table_args__ = (
        Index("ix_trade_outcome_learning_run_id", "run_id"),
        Index("ix_trade_outcome_learning_created_at", "created_at"),
        Index("ix_trade_outcome_learning_approved", "approved"),
        Index("ix_trade_outcome_learning_target", "target_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    decision: Mapped[str] = mapped_column(String(40), nullable=False, default="REJECT_IMPLEMENTATION")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rule_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    trade_outcome_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    temporal_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    asset_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeframe_robustness: Mapped[float | None] = mapped_column(Float, nullable=True)
    generalization_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    simplicity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    overfit_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TradeOutcomeImplementationRun(Base):
    __tablename__ = "trade_outcome_implementation_runs"
    __table_args__ = (
        Index("ix_trade_outcome_impl_run_id", "run_id"),
        Index("ix_trade_outcome_impl_created_at", "created_at"),
        Index("ix_trade_outcome_impl_decision", "decision"),
        Index("ix_trade_outcome_impl_strategy", "strategy_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="OPCAO_B")
    strategy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_name: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    fidelity_precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    fidelity_recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    fidelity_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    overlap_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    false_positives: Mapped[int | None] = mapped_column(Integer, nullable=True)
    false_negatives: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExecutionFrameworkOptimizationRun(Base):
    __tablename__ = "execution_framework_optimization_runs"
    __table_args__ = (
        Index("ix_exec_framework_opt_run_id", "run_id"),
        Index("ix_exec_framework_opt_created_at", "created_at"),
        Index("ix_exec_framework_opt_equivalence", "equivalence_passed"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    strategy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    benchmark_timeframe: Mapped[str | None] = mapped_column(String(20), nullable=True)
    same_trades: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    same_metrics: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    equivalence_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bars_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_before_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_result_before_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_result_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    audit_json: Mapped[str] = mapped_column(Text, nullable=False)
    benchmark_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
