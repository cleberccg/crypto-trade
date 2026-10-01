"""
Modelos ORM do SQLAlchemy.

Decisão de projeto: cada tabela representa um único conceito de negócio.
- Candle: dados brutos de mercado OHLCV.
- Signal: resultado do gerador de sinais de uma estratégia.
- Trade: ciclo de vida de uma posição (aberta → fechada).
- Order: ordens individuais da exchange associadas a uma operação.
- PortfolioSnapshot: avaliação pontual da carteira (usada no backtesting
    e no paper trading).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarativa compartilhada por todos os modelos ORM."""


class Candle(Base):
    """
    Dados brutos de candles OHLCV baixados da exchange.

    A combinação (symbol, timeframe, open_time) é única, para que downloads
    duplicados não criem linhas repetidas.
    """

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candle_symbol_timeframe_open_time", "symbol", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Signal(Base):
    """
    Um sinal de compra/venda/manutenção gerado por uma estratégia.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_execution_id", "execution_id"),
        Index("ix_signals_strategy", "strategy"),
        Index("ix_signals_symbol", "symbol"),
        Index("ix_signals_timeframe", "timeframe"),
        Index("ix_signals_approved", "accepted"),
        Index("ix_signals_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY | SELL | HOLD
    price: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    indicator_snapshot: Mapped[IndicatorSnapshot | None] = relationship(
        "IndicatorSnapshot", back_populates="signal", cascade="all, delete-orphan", uselist=False
    )


class Trade(Base):
    """
    O ciclo de vida completo de uma operação, da entrada à saída.
    """

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_execution_id", "execution_id"),
        Index("ix_trades_strategy", "strategy"),
        Index("ix_trades_symbol", "symbol"),
        Index("ix_trades_timeframe", "timeframe"),
        Index("ix_trades_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(100), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    side: Mapped[str] = mapped_column(String(5), nullable=False)  # BUY | SELL
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # OPEN | CLOSED | CANCELLED
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    stake_amount: Mapped[float] = mapped_column(Float, nullable=False)

    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop: Mapped[float | None] = mapped_column(Float, nullable=True)

    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_fees_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_fee_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_fee_allocated_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fee_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_accounting_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    orders: Mapped[list[Order]] = relationship(
        "Order", back_populates="trade", cascade="all, delete-orphan"
    )


class IndicatorSnapshot(Base):
    """Registro dos indicadores capturado sempre que um sinal BUY ou SELL é armazenado."""

    __tablename__ = "indicator_snapshot"
    __table_args__ = (
        Index("ix_indicator_snapshot_signal_id", "signal_id"),
        Index("ix_indicator_snapshot_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, unique=True
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    signal: Mapped[Signal] = relationship("Signal", back_populates="indicator_snapshot")


class Order(Base):
    """
    Uma ordem individual da exchange vinculada a uma operação.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # MERCADO | LIMITE | STOP
    side: Mapped[str] = mapped_column(String(5), nullable=False)  # BUY | SELL
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fee: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fee_currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fee_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_source: Mapped[str] = mapped_column(String(32), default="MISSING", nullable=False)
    fee_conversion_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_fee_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    trade: Mapped[Trade] = relationship("Trade", back_populates="orders")


class PortfolioSnapshot(Base):
    """
    Registro pontual do valor da carteira, usado para gerar a curva de patrimônio.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_value: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False)
    open_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # paper | Backtest | live
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
