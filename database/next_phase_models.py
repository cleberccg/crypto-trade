"""Non-destructive next-phase ORM models prepared for post-run activation.

These models are additive and not wired to destructive migrations.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base


class ExecutionTimelineEvent(Base):
    __tablename__ = "execution_timeline"
    __table_args__ = (
        Index("ix_execution_timeline_event_type", "event_type"),
        Index("ix_execution_timeline_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NotificationRecordModel(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_channel", "channel"),
        Index("ix_notifications_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="stored")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SchedulerPlanModel(Base):
    __tablename__ = "scheduler_plan"
    __table_args__ = (
        Index("ix_scheduler_plan_enabled", "enabled"),
        Index("ix_scheduler_plan_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    schedule_expr: Mapped[str] = mapped_column(String(140), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExecutionMetricsModel(Base):
    __tablename__ = "execution_metrics"
    __table_args__ = (
        Index("ix_execution_metrics_execution_id", "execution_id"),
        Index("ix_execution_metrics_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")
    total_seconds: Mapped[float | None] = mapped_column(nullable=True)
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    combinations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    combinations_per_second: Mapped[float | None] = mapped_column(nullable=True)
    avg_seconds_per_combination: Mapped[float | None] = mapped_column(nullable=True)
    avg_job_seconds: Mapped[float | None] = mapped_column(nullable=True)
    avg_cpu: Mapped[float | None] = mapped_column(nullable=True)
    max_cpu: Mapped[float | None] = mapped_column(nullable=True)
    avg_ram: Mapped[float | None] = mapped_column(nullable=True)
    max_ram: Mapped[float | None] = mapped_column(nullable=True)
    avg_disk: Mapped[float | None] = mapped_column(nullable=True)
    max_disk: Mapped[float | None] = mapped_column(nullable=True)
    checkpoints: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incidents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recoveries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
