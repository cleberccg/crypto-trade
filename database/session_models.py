"""Execution session and strategy version metadata tables."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        Index("ix_strategy_versions_strategy_name", "strategy_name"),
        Index("ix_strategy_versions_active", "active"),
        Index("ix_strategy_versions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    git_commit: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExecutionSession(Base):
    __tablename__ = "execution_sessions"
    __table_args__ = (
        Index("ix_execution_sessions_execution_id", "execution_id"),
        Index("ix_execution_sessions_status", "status"),
        Index("ix_execution_sessions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    host: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cpu: Mapped[str | None] = mapped_column(String(100), nullable=True)
    workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    python_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    git_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
