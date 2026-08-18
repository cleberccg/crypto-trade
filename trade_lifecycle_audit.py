"""
Trade Lifecycle Audit — FASE 9.3.

Audits the complete position lifecycle to diagnose where performance is being
lost: entry quality, scoring, risk management, position duration, or exit
management.

Usage::

    python main.py trade-lifecycle-audit

"""
from __future__ import annotations

import csv
import json
import math
import os
import platform
import socket
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from database.connection import get_session
from database.history_models import SignalSnapshot, TradeHistory
from database.history_service import HistoryPersistenceService
from database.repositories import CandleRepository
from paper_trading.paper_broker import PaperBroker
from risk.risk_manager import RiskManager
from strategies.factory import create_strategy
from strategies.base_strategy import SignalType
from utils.logger import get_logger

logger = get_logger(__name__)

_TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
    "3d": 4320,
    "1w": 10080,
}


def _tf_minutes(timeframe: str) -> int:
    return _TF_MINUTES.get(timeframe.lower().strip(), 5)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (len(sorted_vals) - 1) * p / 100.0
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_vals):
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _safe(val: float | None, default: float = 0.0) -> float:
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return default
    return float(val)


def _as_utc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TradeLifecycleAuditConfig:
    """Configuration for the trade lifecycle audit."""

    strategy_name: str | None = None
    """Filter by strategy name (None = all strategies)."""

    strategy_version: str | None = None
    """Filter by version suffix (None = all versions)."""

    symbol: str | None = None
    """Filter by symbol, e.g. 'BTC/USDT' (None = all symbols)."""

    timeframe: str | None = None
    """Filter by timeframe, e.g. '5m' (None = auto-detect from trades)."""

    execution_id: str | None = None
    """Filter by execution id (None = latest available)."""

    window_days: int = 30
    """Look-back window in days when no execution_id is specified."""

    output_prefix: str = "trade_lifecycle_audit"
    """Prefix for generated artifact filenames."""

    persist_to_db: bool = True
    """Whether to persist summary to execution_checkpoints."""


@dataclass
class _TradeRecord:
    trade_id: int
    execution_id: str
    strategy: str
    symbol: str
    timeframe: str
    entry_time: datetime
    exit_time: datetime | None
    entry_price: float
    exit_price: float | None
    stop_loss: float | None
    take_profit: float | None
    quantity: float
    pnl: float | None
    pnl_pct: float | None
    duration_minutes: float | None
    exit_reason: str | None
    score: float | None

    @property
    def is_closed(self) -> bool:
        return self.exit_time is not None and self.exit_price is not None

    @property
    def duration_min(self) -> float:
        if self.duration_minutes is not None:
            return float(self.duration_minutes)
        if self.exit_time and self.entry_time:
            return max(0.0, (self.exit_time - self.entry_time).total_seconds() / 60.0)
        return 0.0

    def duration_candles(self, tf_minutes: int) -> float:
        if tf_minutes <= 0:
            return 0.0
        return self.duration_min / tf_minutes


class TradeLifecycleAuditService:
    """
    Audits the full trade lifecycle for position management bottlenecks.

    Runs 7 stages:
    1. Position duration statistics
    2. Exit reason breakdown
    3. Blocked time analysis
    4. Exit quality (MFE/MAE efficiency)
    5. Ideal-time simulations
    6. Operational capacity
    7. Bottleneck diagnosis + recommendation
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._simulated_signals: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, cfg: TradeLifecycleAuditConfig) -> dict[str, Any]:
        started_at = datetime.now(tz=timezone.utc)
        audit_execution_id = HistoryPersistenceService.new_execution_id()
        logger.info("TradeLifecycleAudit starting — execution_id=%s", audit_execution_id)

        # ── Load data ──────────────────────────────────────────────────
        trades = self._load_trades(cfg)
        if not trades:
            logger.warning("No trades found matching the given filters.")
            result: dict[str, Any] = {
                "summary": {"status": "no_data", "message": "No trades found matching filters."},
                "report": {},
                "outputs": {},
            }
            return result

        signals = self._load_signals(cfg, trades)

        # Resolve dominant timeframe
        tf_counter: Counter[str] = Counter(t.timeframe for t in trades)
        dominant_tf = tf_counter.most_common(1)[0][0] if tf_counter else (cfg.timeframe or "5m")
        tf_min = _tf_minutes(dominant_tf)

        closed_trades = [t for t in trades if t.is_closed]
        open_trades = [t for t in trades if not t.is_closed]

        logger.info(
            "TradeLifecycleAudit loaded — total=%d closed=%d open=%d signals=%d tf=%s",
            len(trades),
            len(closed_trades),
            len(open_trades),
            len(signals),
            dominant_tf,
        )

        # ── Load candle data for MFE/MAE ──────────────────────────────
        candle_df = self._load_candles_for_trades(closed_trades, dominant_tf)

        # ── Stage 1 — Duration Statistics ─────────────────────────────
        stage1 = self._stage1_duration(closed_trades, tf_min)

        # ── Stage 2 — Exit Reasons ────────────────────────────────────
        stage2 = self._stage2_exit_reasons(closed_trades)

        # ── Stage 3 — Blocked Time ────────────────────────────────────
        stage3 = self._stage3_blocked_time(closed_trades, signals, tf_min)

        # ── Stage 4 — Exit Quality (MFE / MAE) ───────────────────────
        stage4 = self._stage4_exit_quality(closed_trades, candle_df)

        # ── Stage 5 — Ideal Time Simulations ─────────────────────────
        stage5 = self._stage5_ideal_time(closed_trades, candle_df, tf_min)

        # ── Stage 6 — Operational Capacity ───────────────────────────
        stage6 = self._stage6_operational_capacity(closed_trades, signals, stage5, tf_min)

        # ── Stage 7 — Bottleneck Diagnosis ───────────────────────────
        stage7 = self._stage7_diagnosis(
            trades=trades,
            signals=signals,
            stage1=stage1,
            stage2=stage2,
            stage3=stage3,
            stage4=stage4,
            stage5=stage5,
            stage6=stage6,
        )

        report = {
            "audit_execution_id": audit_execution_id,
            "generated_at": started_at.isoformat(),
            "filters": {
                "strategy_name": cfg.strategy_name,
                "strategy_version": cfg.strategy_version,
                "symbol": cfg.symbol,
                "timeframe": cfg.timeframe,
                "execution_id": cfg.execution_id,
                "window_days": cfg.window_days,
            },
            "data": {
                "total_trades": len(trades),
                "closed_trades": len(closed_trades),
                "open_trades": len(open_trades),
                "total_signals": len(signals),
                "dominant_timeframe": dominant_tf,
                "timeframe_minutes": tf_min,
            },
            "stage1_duration": stage1,
            "stage2_exit_reasons": stage2,
            "stage3_blocked_time": stage3,
            "stage4_exit_quality": stage4,
            "stage5_ideal_time": stage5,
            "stage6_operational_capacity": stage6,
            "stage7_diagnosis": stage7,
        }

        # ── Persist artifacts ─────────────────────────────────────────
        outputs = self._persist_artifacts(cfg, report, closed_trades, audit_execution_id)

        if cfg.persist_to_db:
            self._persist_db(audit_execution_id, cfg, report, started_at)

        summary = {
            "status": "completed",
            "audit_execution_id": audit_execution_id,
            "total_trades": len(trades),
            "closed_trades": len(closed_trades),
            "avg_duration_candles": stage1.get("candles", {}).get("mean", 0),
            "avg_duration_hours": stage1.get("hours", {}).get("mean", 0),
            "blocked_pct": stage3.get("blocked_pct_total", 0),
            "avg_setups_blocked_per_trade": stage3.get("avg_setups_blocked_per_trade", 0),
            "exit_efficiency_mean": stage4.get("exit_efficiency_mean", 0),
            "early_exit_label": stage4.get("diagnosis_label", "N/A"),
            "main_bottleneck": stage7.get("main_bottleneck", "unknown"),
            "recommendation": stage7.get("recommendation", []),
        }

        logger.info(
            "TradeLifecycleAudit completed — bottleneck=%s efficiency=%.2f%% blocked=%.1f%%",
            stage7.get("main_bottleneck"),
            _safe(stage4.get("exit_efficiency_mean")) * 100,
            _safe(stage3.get("blocked_pct_total")),
        )

        return {"summary": summary, "report": report, "outputs": outputs}

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_trades(self, cfg: TradeLifecycleAuditConfig) -> list[_TradeRecord]:
        records = self._load_trades_from_db(cfg)
        if records:
            return records

        logger.info("No trade_history records found; running fallback lifecycle replay from candles.")
        return self._simulate_trades_from_candles(cfg)

    def _load_trades_from_db(self, cfg: TradeLifecycleAuditConfig) -> list[_TradeRecord]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max(1, cfg.window_days))
        with get_session() as session:
            q = session.query(TradeHistory)
            if cfg.execution_id:
                q = q.filter(TradeHistory.execution_id == cfg.execution_id)
            else:
                q = q.filter(TradeHistory.entry_time >= cutoff)
            if cfg.strategy_name:
                q = q.filter(TradeHistory.strategy.like(f"%{cfg.strategy_name}%"))
            if cfg.strategy_version:
                q = q.filter(TradeHistory.strategy.like(f"%{cfg.strategy_version}%"))
            if cfg.symbol:
                q = q.filter(TradeHistory.symbol == cfg.symbol)
            if cfg.timeframe:
                q = q.filter(TradeHistory.timeframe == cfg.timeframe)
            rows = q.order_by(TradeHistory.entry_time.asc()).all()

        records: list[_TradeRecord] = []
        for row in rows:
            records.append(
                _TradeRecord(
                    trade_id=int(row.id),
                    execution_id=str(row.execution_id),
                    strategy=str(row.strategy),
                    symbol=str(row.symbol),
                    timeframe=str(row.timeframe),
                    entry_time=row.entry_time,
                    exit_time=row.exit_time,
                    entry_price=float(row.entry_price),
                    exit_price=float(row.exit_price) if row.exit_price is not None else None,
                    stop_loss=float(row.stop_loss) if row.stop_loss is not None else None,
                    take_profit=float(row.take_profit) if row.take_profit is not None else None,
                    quantity=float(row.quantity),
                    pnl=float(row.pnl) if row.pnl is not None else None,
                    pnl_pct=float(row.pnl_percent) if row.pnl_percent is not None else None,
                    duration_minutes=float(row.duration_minutes) if row.duration_minutes is not None else None,
                    exit_reason=str(row.exit_reason) if row.exit_reason else None,
                    score=float(row.score) if row.score is not None else None,
                )
            )
        return records

    def _simulate_trades_from_candles(self, cfg: TradeLifecycleAuditConfig) -> list[_TradeRecord]:
        state_file = self._results_dir / "paper_live_state.json"
        state: dict[str, Any] = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                state = {}

        symbol = cfg.symbol or str(state.get("symbol") or "BTC/USDT")
        timeframe = cfg.timeframe or str(state.get("timeframe") or "5m")
        strategy_name = cfg.strategy_name or str(state.get("strategy_name") or "TradeOutcomeNextGenV1")
        strategy_version = cfg.strategy_version or str(state.get("strategy_version") or "v1.0")
        execution_id = cfg.execution_id or str(state.get("execution_id") or HistoryPersistenceService.new_execution_id())

        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(1, cfg.window_days))

        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start_dt, end_dt)

        if not candles:
            logger.warning("Fallback replay could not load candles for %s/%s.", symbol, timeframe)
            return []

        df = pd.DataFrame(
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

        strategy = create_strategy(strategy_name)
        strategy.initialize()
        risk_manager = RiskManager()
        broker = PaperBroker(initial_capital=10_000.0)

        self._simulated_signals = []
        trades: list[_TradeRecord] = []
        open_pos: dict[str, Any] | None = None
        highest = 0.0
        warmup = min(50, max(0, len(df) - 1))

        for i in range(warmup, len(df)):
            window = df.iloc[: i + 1]
            enriched = strategy.calculate(window)
            last = enriched.iloc[-1]
            current_price = float(last["close"])
            ts: datetime = last.name.to_pydatetime()  # type: ignore[union-attr]

            if open_pos is not None:
                highest = max(highest, current_price)
                stop_hit = current_price <= float(open_pos["stop_loss"])
                tp_hit = current_price >= float(open_pos["take_profit"])
                trailing = float(open_pos.get("trailing_stop") or 0.0)
                trailing_hit = trailing > 0 and risk_manager.check_trailing_stop(
                    float(open_pos["entry_price"]),
                    current_price,
                    highest,
                    trailing,
                )
                exit_sig = strategy.exit_signal(enriched, float(open_pos["entry_price"]))

                exit_reason: str | None = None
                exit_price = current_price
                if stop_hit:
                    exit_reason = "stop_loss"
                    exit_price = float(open_pos["stop_loss"])
                elif tp_hit:
                    exit_reason = "take_profit"
                    exit_price = float(open_pos["take_profit"])
                elif trailing_hit:
                    exit_reason = "trailing_stop"
                elif exit_sig.signal == SignalType.SELL:
                    exit_reason = str(exit_sig.metadata.get("exit_reason", "strategy_exit"))

                if exit_reason is not None:
                    broker.create_market_sell(symbol, float(open_pos["quantity"]), exit_price)
                    pnl = (exit_price - float(open_pos["entry_price"])) * float(open_pos["quantity"])
                    stake = float(open_pos["stake_amount"])
                    pnl_pct = pnl / stake if stake > 0 else 0.0
                    dur_min = max(0.0, (ts - open_pos["entry_time"]).total_seconds() / 60.0)

                    trades.append(
                        _TradeRecord(
                            trade_id=len(trades) + 1,
                            execution_id=execution_id,
                            strategy=f"{strategy_name}@{strategy_version}",
                            symbol=symbol,
                            timeframe=timeframe,
                            entry_time=open_pos["entry_time"],
                            exit_time=ts,
                            entry_price=float(open_pos["entry_price"]),
                            exit_price=float(exit_price),
                            stop_loss=float(open_pos["stop_loss"]),
                            take_profit=float(open_pos["take_profit"]),
                            quantity=float(open_pos["quantity"]),
                            pnl=float(pnl),
                            pnl_pct=float(pnl_pct),
                            duration_minutes=float(dur_min),
                            exit_reason=exit_reason,
                            score=float(open_pos.get("score") or 0.0),
                        )
                    )
                    open_pos = None
                    highest = 0.0

            if open_pos is None:
                entry_sig = strategy.entry_signal(enriched)
                if entry_sig.signal == SignalType.BUY:
                    accepted = False
                    rejection_reason: str | None = None
                    try:
                        strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(strategy)
                        if strategy_rr_min is None:
                            strategy_rr_min = RiskManager.infer_min_risk_reward_ratio_from_levels(
                                entry_price=current_price,
                                stop_loss=entry_sig.stop_loss,
                                take_profit=entry_sig.take_profit,
                            )
                        risk = risk_manager.evaluate_trade(
                            portfolio_value=broker.get_balance().cash,
                            entry_price=current_price,
                            stop_loss=entry_sig.stop_loss,
                            take_profit=entry_sig.take_profit,
                            trailing_stop_pct=entry_sig.trailing_stop_pct,
                            strategy_score=entry_sig.score,
                            min_risk_reward_ratio=strategy_rr_min,
                        )
                        broker.create_market_buy(symbol, float(risk.quantity), current_price)
                        open_pos = {
                            "entry_time": ts,
                            "entry_price": current_price,
                            "quantity": float(risk.quantity),
                            "stake_amount": float(risk.stake_amount),
                            "stop_loss": float(risk.stop_loss),
                            "take_profit": float(risk.take_profit),
                            "trailing_stop": float(risk.trailing_stop_pct or 0.0),
                            "score": float(entry_sig.score or 0.0),
                        }
                        highest = current_price
                        accepted = True
                    except Exception as exc:
                        rejection_reason = str(exc)

                    self._simulated_signals.append(
                        {
                            "id": len(self._simulated_signals) + 1,
                            "execution_id": execution_id,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "timestamp": ts,
                            "signal": "BUY",
                            "score": float(entry_sig.score or 0.0),
                            "accepted": accepted,
                            "rejection_reason": rejection_reason,
                        }
                    )

        logger.info(
            "Fallback replay generated %d closed trades and %d signals.",
            len(trades),
            len(self._simulated_signals),
        )
        return trades

    def _load_signals(
        self,
        cfg: TradeLifecycleAuditConfig,
        trades: list[_TradeRecord],
    ) -> list[dict[str, Any]]:
        if not trades:
            return []
        cutoff = min(t.entry_time for t in trades)
        with get_session() as session:
            q = session.query(SignalSnapshot)
            if cfg.execution_id:
                q = q.filter(SignalSnapshot.execution_id == cfg.execution_id)
            else:
                q = q.filter(SignalSnapshot.timestamp >= cutoff)
            if cfg.strategy_name:
                q = q.filter(SignalSnapshot.strategy.like(f"%{cfg.strategy_name}%"))
            if cfg.symbol:
                q = q.filter(SignalSnapshot.symbol == cfg.symbol)
            if cfg.timeframe:
                q = q.filter(SignalSnapshot.timeframe == cfg.timeframe)
            rows = q.order_by(SignalSnapshot.timestamp.asc()).all()

        if not rows and self._simulated_signals:
            return list(self._simulated_signals)

        return [
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "timestamp": _as_utc_dt(row.timestamp),
                "signal": row.signal,
                "score": row.score,
                "accepted": bool(row.accepted),
                "rejection_reason": row.rejection_reason,
            }
            for row in rows
        ]

    def _load_candles_for_trades(
        self,
        trades: list[_TradeRecord],
        timeframe: str,
    ) -> pd.DataFrame:
        if not trades:
            return pd.DataFrame()

        # Collect all unique symbol/timeframe combos and their time ranges
        groups: dict[tuple[str, str], tuple[datetime, datetime]] = {}
        for t in trades:
            if not t.is_closed:
                continue
            key = (t.symbol, t.timeframe or timeframe)
            entry = t.entry_time
            exit_ = t.exit_time  # type: ignore[assignment]
            if key in groups:
                prev_start, prev_end = groups[key]
                groups[key] = (min(prev_start, entry), max(prev_end, exit_))
            else:
                groups[key] = (entry, exit_)

        frames: list[pd.DataFrame] = []
        with get_session() as session:
            repo = CandleRepository(session)
            for (sym, tf), (start, end) in groups.items():
                try:
                    candles = repo.get_range(sym, tf, start - timedelta(minutes=5), end + timedelta(minutes=5))
                    if not candles:
                        continue
                    df = pd.DataFrame(
                        [
                            {
                                "symbol": sym,
                                "timeframe": tf,
                                "open_time": c.open_time,
                                "high": c.high,
                                "low": c.low,
                                "close": c.close,
                            }
                            for c in candles
                        ]
                    )
                    frames.append(df)
                except Exception as exc:
                    logger.warning("Failed to load candles for %s/%s: %s", sym, tf, exc)

        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True)
        merged["open_time"] = pd.to_datetime(merged["open_time"], utc=True)
        return merged

    # ------------------------------------------------------------------
    # Stage 1 — Duration Statistics
    # ------------------------------------------------------------------

    def _stage1_duration(
        self,
        trades: list[_TradeRecord],
        tf_min: int,
    ) -> dict[str, Any]:
        durations_min = [t.duration_min for t in trades if t.duration_min > 0]
        durations_candles = [d / max(1, tf_min) for d in durations_min]
        durations_hours = [d / 60.0 for d in durations_min]

        def _stats(values: list[float]) -> dict[str, float]:
            if not values:
                return {k: 0.0 for k in ["mean", "median", "min", "max", "p50", "p75", "p90", "p95"]}
            return {
                "mean": sum(values) / len(values),
                "median": _percentile(values, 50),
                "min": min(values),
                "max": max(values),
                "p50": _percentile(values, 50),
                "p75": _percentile(values, 75),
                "p90": _percentile(values, 90),
                "p95": _percentile(values, 95),
            }

        return {
            "trade_count": len(durations_min),
            "minutes": _stats(durations_min),
            "candles": _stats(durations_candles),
            "hours": _stats(durations_hours),
        }

    # ------------------------------------------------------------------
    # Stage 2 — Exit Reasons
    # ------------------------------------------------------------------

    def _stage2_exit_reasons(self, trades: list[_TradeRecord]) -> dict[str, Any]:
        counter: Counter[str] = Counter()
        for t in trades:
            reason = t.exit_reason or "unknown"
            counter[reason] += 1

        total = len(trades)
        breakdown: list[dict[str, Any]] = []
        for reason, count in counter.most_common():
            breakdown.append(
                {
                    "reason": reason,
                    "count": count,
                    "pct": round(count / total * 100, 2) if total > 0 else 0.0,
                }
            )

        return {"total_closed": total, "breakdown": breakdown}

    # ------------------------------------------------------------------
    # Stage 3 — Blocked Time
    # ------------------------------------------------------------------

    def _stage3_blocked_time(
        self,
        trades: list[_TradeRecord],
        signals: list[dict[str, Any]],
        tf_min: int,
    ) -> dict[str, Any]:
        # Count signals that were blocked by an open position (rejection_reason contains
        # common patterns: "posicao_existente", "already_open", "position_open", etc.)
        _blocked_keywords = ("posicao_existente", "already_open", "position_open", "ja existe")
        buy_signals = [s for s in signals if str(s.get("signal", "")).upper() == "BUY"]
        blocked_by_position = [
            s for s in buy_signals
            if not s.get("accepted", True)
            and any(kw in str(s.get("rejection_reason", "")).lower() for kw in _blocked_keywords)
        ]
        other_rejected = [
            s for s in buy_signals
            if not s.get("accepted", True) and s not in blocked_by_position
        ]
        accepted_entries = [s for s in buy_signals if s.get("accepted", False)]

        total_buy_signals = len(buy_signals)
        n_blocked = len(blocked_by_position)
        n_trades = len(trades)

        # Blocked duration in candles
        total_blocked_candles = sum(t.duration_candles(tf_min) for t in trades)

        # Time window of the whole dataset
        if trades:
            first_entry = min(t.entry_time for t in trades)
            last_exit = max(
                (t.exit_time for t in trades if t.exit_time),
                default=first_entry,
            )
            total_window_minutes = max(1.0, (last_exit - first_entry).total_seconds() / 60.0)
            total_window_candles = total_window_minutes / max(1, tf_min)

            total_blocked_minutes = sum(t.duration_min for t in trades)
            blocked_pct_total = total_blocked_minutes / total_window_minutes * 100.0
        else:
            total_window_candles = 0.0
            blocked_pct_total = 0.0
            total_blocked_minutes = 0.0

        avg_setups_blocked_per_trade = n_blocked / max(1, n_trades) if n_blocked > 0 else 0.0

        # Also compute using signal timestamps: for each trade, count buy signals
        # that arrived during the position window
        setups_per_trade: list[int] = []
        for t in trades:
            if not t.is_closed:
                continue
            entry_dt = _as_utc_dt(t.entry_time)
            exit_dt = _as_utc_dt(t.exit_time or t.entry_time)
            count = sum(
                1 for s in buy_signals
                if entry_dt <= _as_utc_dt(s["timestamp"]) <= exit_dt
                and not s.get("accepted", False)
            )
            setups_per_trade.append(count)

        avg_blocked_per_trade_ts = (
            sum(setups_per_trade) / len(setups_per_trade) if setups_per_trade else 0.0
        )

        return {
            "total_buy_signals": total_buy_signals,
            "accepted_entries": len(accepted_entries),
            "blocked_by_position": n_blocked,
            "other_rejected": len(other_rejected),
            "total_blocked_minutes": round(total_blocked_minutes, 1),
            "total_blocked_candles": round(total_blocked_candles, 1),
            "blocked_pct_total": round(blocked_pct_total, 2),
            "avg_setups_blocked_per_trade": round(avg_setups_blocked_per_trade, 2),
            "avg_blocked_per_trade_ts": round(avg_blocked_per_trade_ts, 2),
            "interpretation": (
                f"Cada trade bloqueou em média "
                f"{avg_setups_blocked_per_trade:.1f} novos setups (via reason) / "
                f"{avg_blocked_per_trade_ts:.1f} (via timestamp). "
                f"Total de tempo bloqueado: {blocked_pct_total:.1f}% do período."
            ),
        }

    # ------------------------------------------------------------------
    # Stage 4 — Exit Quality (MFE / MAE)
    # ------------------------------------------------------------------

    def _stage4_exit_quality(
        self,
        trades: list[_TradeRecord],
        candle_df: pd.DataFrame,
    ) -> dict[str, Any]:
        trade_details: list[dict[str, Any]] = []

        for t in trades:
            if not t.is_closed or t.entry_price <= 0:
                continue

            mfe = 0.0
            mae = 0.0

            if not candle_df.empty:
                entry_dt = _as_utc_dt(t.entry_time)
                exit_dt = _as_utc_dt(t.exit_time or t.entry_time)
                mask = (
                    (candle_df["symbol"] == t.symbol)
                    & (candle_df["open_time"] >= entry_dt)
                    & (candle_df["open_time"] <= exit_dt)
                )
                window = candle_df[mask]
                if not window.empty:
                    mfe = float((window["high"].max() - t.entry_price) / t.entry_price)
                    mae = float((t.entry_price - window["low"].min()) / t.entry_price)

            realized = _safe(t.pnl_pct)
            # Exit efficiency: what fraction of MFE was captured
            efficiency = realized / mfe if mfe > 1e-10 else (1.0 if realized >= 0 else 0.0)
            efficiency = max(-2.0, min(2.0, efficiency))

            trade_details.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,  # type: ignore[union-attr]
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl_pct": round(realized * 100, 4),
                    "mfe_pct": round(mfe * 100, 4),
                    "mae_pct": round(mae * 100, 4),
                    "exit_efficiency": round(efficiency, 4),
                    "exit_reason": t.exit_reason,
                }
            )

        if not trade_details:
            return {
                "trade_count": 0,
                "exit_efficiency_mean": 0.0,
                "exit_efficiency_median": 0.0,
                "mfe_mean_pct": 0.0,
                "mae_mean_pct": 0.0,
                "diagnosis_label": "insufficient_data",
                "trade_details": [],
            }

        efficiencies = [d["exit_efficiency"] for d in trade_details]
        mfe_vals = [d["mfe_pct"] for d in trade_details]
        mae_vals = [d["mae_pct"] for d in trade_details]

        eff_mean = sum(efficiencies) / len(efficiencies)
        eff_median = _percentile(efficiencies, 50)

        # Diagnosis: are we exiting too early or too late?
        early_exits = sum(1 for e in efficiencies if e < 0.5)
        late_exits = sum(1 for e in efficiencies if e > 1.2)
        optimal = len(efficiencies) - early_exits - late_exits

        if early_exits > late_exits and early_exits > optimal:
            label = "saindo_cedo_demais"
        elif late_exits > early_exits and late_exits > optimal:
            label = "saindo_tarde_demais"
        elif eff_mean >= 0.65:
            label = "saida_adequada"
        else:
            label = "saida_subotima"

        return {
            "trade_count": len(trade_details),
            "exit_efficiency_mean": round(eff_mean, 4),
            "exit_efficiency_median": round(eff_median, 4),
            "mfe_mean_pct": round(sum(mfe_vals) / max(1, len(mfe_vals)), 4),
            "mae_mean_pct": round(sum(mae_vals) / max(1, len(mae_vals)), 4),
            "early_exits": early_exits,
            "late_exits": late_exits,
            "optimal_exits": optimal,
            "diagnosis_label": label,
            "interpretation": (
                f"Eficiência média de saída: {eff_mean * 100:.1f}%. "
                f"Saídas precoces (<50% MFE): {early_exits}. "
                f"Saídas tardias (>120% MFE): {late_exits}. "
                f"Diagnóstico: {label}."
            ),
            "trade_details": trade_details,
        }

    # ------------------------------------------------------------------
    # Stage 5 — Ideal Time Simulations
    # ------------------------------------------------------------------

    def _stage5_ideal_time(
        self,
        trades: list[_TradeRecord],
        candle_df: pd.DataFrame,
        tf_min: int,
    ) -> dict[str, Any]:
        scenarios: dict[str, dict[str, Any]] = {
            "baseline": {"description": "Saída original (baseline)", "pnls": [], "wins": 0},
            "exit_25pct_earlier": {"description": "Saída 25% mais cedo", "pnls": [], "wins": 0},
            "exit_50pct_earlier": {"description": "Saída 50% mais cedo", "pnls": [], "wins": 0},
            "time_stop_4h": {"description": "Time Stop 48 candles (4h em 5m)", "pnls": [], "wins": 0},
            "reversal_exit": {"description": "Saída no pico (MFE)", "pnls": [], "wins": 0},
        }

        time_stop_candles = max(1, round(240 / max(1, tf_min)))  # 4 hours in candles

        for t in trades:
            if not t.is_closed or t.entry_price <= 0:
                continue

            baseline_pnl = _safe(t.pnl_pct)
            scenarios["baseline"]["pnls"].append(baseline_pnl)
            if baseline_pnl > 0:
                scenarios["baseline"]["wins"] = int(scenarios["baseline"]["wins"]) + 1

            # Load this trade's candle window
            trade_candles = pd.DataFrame()
            if not candle_df.empty:
                entry_dt = _as_utc_dt(t.entry_time)
                exit_dt = _as_utc_dt(t.exit_time or t.entry_time)
                mask = (
                    (candle_df["symbol"] == t.symbol)
                    & (candle_df["open_time"] >= entry_dt)
                    & (candle_df["open_time"] <= exit_dt)
                )
                trade_candles = candle_df[mask].copy()

            total_candles = max(1, len(trade_candles))

            # Scenario: 25% earlier — exit at 75% of original duration
            idx_25pct = max(1, round(total_candles * 0.75))
            pnl_25 = self._simulate_exit_at_candle(t, trade_candles, idx_25pct - 1)
            scenarios["exit_25pct_earlier"]["pnls"].append(pnl_25)
            if pnl_25 > 0:
                scenarios["exit_25pct_earlier"]["wins"] = int(scenarios["exit_25pct_earlier"]["wins"]) + 1

            # Scenario: 50% earlier — exit at 50% of original duration
            idx_50pct = max(1, round(total_candles * 0.50))
            pnl_50 = self._simulate_exit_at_candle(t, trade_candles, idx_50pct - 1)
            scenarios["exit_50pct_earlier"]["pnls"].append(pnl_50)
            if pnl_50 > 0:
                scenarios["exit_50pct_earlier"]["wins"] = int(scenarios["exit_50pct_earlier"]["wins"]) + 1

            # Scenario: time stop at fixed candle count
            pnl_time = self._simulate_exit_at_candle(t, trade_candles, time_stop_candles - 1)
            scenarios["time_stop_4h"]["pnls"].append(pnl_time)
            if pnl_time > 0:
                scenarios["time_stop_4h"]["wins"] = int(scenarios["time_stop_4h"]["wins"]) + 1

            # Scenario: exit at peak (MFE candle)
            pnl_mfe = self._simulate_mfe_exit(t, trade_candles)
            scenarios["reversal_exit"]["pnls"].append(pnl_mfe)
            if pnl_mfe > 0:
                scenarios["reversal_exit"]["wins"] = int(scenarios["reversal_exit"]["wins"]) + 1

        results: list[dict[str, Any]] = []
        for key, s in scenarios.items():
            pnls: list[float] = s["pnls"]  # type: ignore[assignment]
            n = len(pnls)
            total_pnl = sum(pnls)
            avg_pnl = total_pnl / max(1, n)
            win_rate = int(s["wins"]) / max(1, n)
            results.append(
                {
                    "scenario": key,
                    "description": s["description"],
                    "trade_count": n,
                    "total_pnl_pct": round(total_pnl * 100, 4),
                    "avg_pnl_pct": round(avg_pnl * 100, 4),
                    "win_rate": round(win_rate, 4),
                    "wins": int(s["wins"]),
                }
            )

        return {
            "note": "Simulações calculadas sobre trades existentes sem alterar a estratégia.",
            "time_stop_candles_used": time_stop_candles,
            "scenarios": results,
        }

    def _simulate_exit_at_candle(
        self,
        trade: _TradeRecord,
        candles: pd.DataFrame,
        candle_idx: int,
    ) -> float:
        """Return pnl_pct at a given candle index, falling back to actual exit."""
        if candles.empty or candle_idx >= len(candles):
            return _safe(trade.pnl_pct)
        exit_price = float(candles.iloc[candle_idx]["close"])
        return (exit_price - trade.entry_price) / trade.entry_price

    def _simulate_mfe_exit(
        self,
        trade: _TradeRecord,
        candles: pd.DataFrame,
    ) -> float:
        """Return pnl_pct if we had exited at the maximum high (best possible)."""
        if candles.empty:
            return _safe(trade.pnl_pct)
        best_high = float(candles["high"].max())
        return (best_high - trade.entry_price) / trade.entry_price

    # ------------------------------------------------------------------
    # Stage 6 — Operational Capacity
    # ------------------------------------------------------------------

    def _stage6_operational_capacity(
        self,
        trades: list[_TradeRecord],
        signals: list[dict[str, Any]],
        stage5: dict[str, Any],
        tf_min: int,
    ) -> dict[str, Any]:
        n_trades = len(trades)
        if n_trades == 0:
            return {
                "current_trades": 0,
                "freq_per_day": 0.0,
                "scenarios": [],
                "interpretation": "Sem dados suficientes.",
            }

        # Total time span
        first_entry = min(t.entry_time for t in trades)
        last_exit_candidates = [t.exit_time for t in trades if t.exit_time]
        last_exit = max(last_exit_candidates) if last_exit_candidates else first_entry
        total_days = max(1.0, (last_exit - first_entry).total_seconds() / 86400.0)

        freq_per_day = n_trades / total_days

        # Average duration
        avg_dur_min = sum(t.duration_min for t in trades) / max(1, n_trades)
        avg_dur_candles = avg_dur_min / max(1, tf_min)

        # Rejected buy signals (excluding position blocks)
        buy_signals = [s for s in signals if str(s.get("signal", "")).upper() == "BUY"]
        _blocked_keywords = ("posicao_existente", "already_open", "position_open", "ja existe")
        blocked = [
            s for s in buy_signals
            if not s.get("accepted", False)
            and any(kw in str(s.get("rejection_reason", "")).lower() for kw in _blocked_keywords)
        ]
        n_blocked = len(blocked)

        # Estimate additional trades if we shorten duration by different fractions
        scenarios_out: list[dict[str, Any]] = []
        for fraction, label in [(0.25, "25% mais cedo"), (0.50, "50% mais cedo"), (1.0, "time_stop_4h")]:
            if fraction == 1.0:
                # 4 hours in minutes
                new_avg_dur = min(avg_dur_min, 240.0)
            else:
                new_avg_dur = avg_dur_min * (1.0 - fraction)
            new_avg_dur = max(tf_min, new_avg_dur)  # at least 1 candle

            # Time freed: each trade is shorter by (avg_dur_min - new_avg_dur)
            freed_minutes_per_trade = avg_dur_min - new_avg_dur
            total_freed_minutes = freed_minutes_per_trade * n_trades
            additional_trades = total_freed_minutes / max(1.0, avg_dur_min)

            # Additional setups that could be captured = min(blocked, additional_trades)
            captured = min(n_blocked, additional_trades)
            new_freq_per_day = (n_trades + captured) / total_days

            scenarios_out.append(
                {
                    "scenario": label,
                    "new_avg_duration_min": round(new_avg_dur, 1),
                    "additional_trades_est": round(additional_trades, 1),
                    "additional_setups_captured": round(captured, 1),
                    "new_total_trades_est": round(n_trades + captured, 1),
                    "new_freq_per_day": round(new_freq_per_day, 2),
                    "freq_increase_pct": round((new_freq_per_day / max(1e-6, freq_per_day) - 1) * 100, 1),
                }
            )

        return {
            "current_trades": n_trades,
            "total_days_analyzed": round(total_days, 1),
            "freq_per_day": round(freq_per_day, 3),
            "avg_duration_min": round(avg_dur_min, 1),
            "avg_duration_candles": round(avg_dur_candles, 1),
            "blocked_setups_total": n_blocked,
            "scenarios": scenarios_out,
            "interpretation": (
                f"{n_trades} trades em {total_days:.0f} dias = {freq_per_day:.2f} trades/dia. "
                f"{n_blocked} setups bloqueados por posição aberta."
            ),
        }

    # ------------------------------------------------------------------
    # Stage 7 — Bottleneck Diagnosis
    # ------------------------------------------------------------------

    def _stage7_diagnosis(
        self,
        trades: list[_TradeRecord],
        signals: list[dict[str, Any]],
        stage1: dict[str, Any],
        stage2: dict[str, Any],
        stage3: dict[str, Any],
        stage4: dict[str, Any],
        stage5: dict[str, Any],
        stage6: dict[str, Any],
    ) -> dict[str, Any]:
        evidence: list[str] = []
        scores: dict[str, float] = {
            "entrada": 0.0,
            "score": 0.0,
            "risk_manager": 0.0,
            "gestao_posicao": 0.0,
            "gestao_saida": 0.0,
        }

        # Evidence: blocked time
        blocked_pct = _safe(stage3.get("blocked_pct_total"))
        if blocked_pct > 50:
            evidence.append(f"Estratégia ficou bloqueada {blocked_pct:.1f}% do tempo total.")
            scores["gestao_posicao"] += 40.0

        avg_setups_blocked = _safe(stage3.get("avg_setups_blocked_per_trade"))
        if avg_setups_blocked > 5:
            evidence.append(f"Cada trade bloqueou em média {avg_setups_blocked:.1f} novos setups.")
            scores["gestao_posicao"] += 30.0
        elif avg_setups_blocked > 2:
            scores["gestao_posicao"] += 15.0

        # Evidence: exit quality
        eff_mean = _safe(stage4.get("exit_efficiency_mean"))
        diag_label = stage4.get("diagnosis_label", "")
        if diag_label == "saindo_cedo_demais":
            evidence.append(f"Saídas precoces detectadas (eficiência média: {eff_mean * 100:.1f}%).")
            scores["gestao_saida"] += 35.0
        elif diag_label == "saindo_tarde_demais":
            evidence.append(f"Saídas tardias detectadas (eficiência média: {eff_mean * 100:.1f}%).")
            scores["gestao_saida"] += 30.0
        elif eff_mean < 0.4:
            evidence.append(f"Eficiência de saída baixa: {eff_mean * 100:.1f}%.")
            scores["gestao_saida"] += 25.0

        # Evidence: duration
        avg_dur_h = _safe(stage1.get("hours", {}).get("mean"))
        p90_h = _safe(stage1.get("hours", {}).get("p90"))
        if avg_dur_h > 8:
            evidence.append(f"Duração média das posições é alta: {avg_dur_h:.1f}h (P90={p90_h:.1f}h).")
            scores["gestao_posicao"] += 20.0

        # Evidence: signal acceptance
        total_buy = int(stage3.get("total_buy_signals", 0))
        accepted = int(stage3.get("accepted_entries", 0))
        total_trades = len(trades)
        if total_buy > 0 and accepted / total_buy < 0.02:
            evidence.append(f"Taxa de aceitação muito baixa: {accepted}/{total_buy} ({accepted / total_buy * 100:.1f}%).")
            scores["score"] += 10.0
            scores["risk_manager"] += 10.0

        # Simulation comparison
        scenarios = stage5.get("scenarios", [])
        baseline_pnl = next((s["total_pnl_pct"] for s in scenarios if s["scenario"] == "baseline"), 0.0)
        mfe_pnl = next((s["total_pnl_pct"] for s in scenarios if s["scenario"] == "reversal_exit"), 0.0)
        if mfe_pnl > baseline_pnl * 1.5 and baseline_pnl > 0:
            evidence.append(
                f"Saída no pico (MFE) geraria +{mfe_pnl - baseline_pnl:.1f}pp vs baseline → grande upside em melhora de saída."
            )
            scores["gestao_saida"] += 20.0

        # Determine main bottleneck
        main_bottleneck = max(scores, key=lambda k: scores[k]) if scores else "gestao_posicao"
        if scores[main_bottleneck] == 0:
            main_bottleneck = "gestao_posicao"

        # Recommendations
        recommendation: list[str] = []
        if main_bottleneck in ("gestao_posicao", "gestao_saida"):
            if avg_dur_h > 4:
                recommendation.append(
                    f"Adicionar time stop de {max(1, round(avg_dur_h * 0.5)):.0f}h para reduzir permanência média."
                )
            if diag_label == "saindo_cedo_demais":
                recommendation.append("Revisar critério de saída: trailing stop muito agressivo ou take profit muito próximo.")
            elif diag_label == "saindo_tarde_demais":
                recommendation.append("Adicionar exit por reversão de momentum para capturar mais do MFE.")
            if blocked_pct > 40:
                recommendation.append(
                    "Reduzir duração média das posições para liberar capital e capturar mais oportunidades."
                )
            if avg_setups_blocked > 3:
                recommendation.append(
                    "Avaliar allow-reentry em condições específicas ou permitir múltiplas posições em tamanhos reduzidos."
                )

        if not recommendation:
            recommendation.append(
                "Dados insuficientes para recomendação específica. Coletar mais trades e re-executar o audit."
            )

        summary_text = (
            f"Bottleneck principal identificado: {main_bottleneck.upper()}. "
            f"Score de evidências: {json.dumps({k: round(v, 1) for k, v in scores.items()})}. "
            f"Trades analisados: {total_trades}. "
            f"Eficiência de saída: {eff_mean * 100:.1f}%. "
            f"Tempo bloqueado: {blocked_pct:.1f}%."
        )

        return {
            "bottleneck_scores": {k: round(v, 1) for k, v in scores.items()},
            "main_bottleneck": main_bottleneck,
            "evidence": evidence,
            "recommendation": recommendation,
            "summary": summary_text,
            "note": "Nenhuma alteração foi feita na estratégia. Este é apenas um diagnóstico.",
        }

    # ------------------------------------------------------------------
    # Artifact persistence
    # ------------------------------------------------------------------

    def _persist_artifacts(
        self,
        cfg: TradeLifecycleAuditConfig,
        report: dict[str, Any],
        closed_trades: list[_TradeRecord],
        audit_id: str,
    ) -> dict[str, str]:
        prefix = cfg.output_prefix
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        # JSON
        json_path = self._results_dir / f"{prefix}_{ts}.json"
        json_path.write_text(
            json.dumps(report, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        outputs["json"] = str(json_path)

        # CSV — trade-level MFE/MAE details
        trade_details = report.get("stage4_exit_quality", {}).get("trade_details", [])
        if trade_details:
            csv_path = self._results_dir / f"{prefix}_{ts}_trades.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(trade_details[0].keys()))
                writer.writeheader()
                writer.writerows(trade_details)
            outputs["csv_trades"] = str(csv_path)

        # CSV — simulation scenarios
        scenarios = report.get("stage5_ideal_time", {}).get("scenarios", [])
        if scenarios:
            sim_csv_path = self._results_dir / f"{prefix}_{ts}_simulations.csv"
            with sim_csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(scenarios[0].keys()))
                writer.writeheader()
                writer.writerows(scenarios)
            outputs["csv_simulations"] = str(sim_csv_path)

        # Markdown report
        md_path = self._results_dir / f"{prefix}_{ts}.md"
        md_path.write_text(
            self._build_markdown(report),
            encoding="utf-8",
        )
        outputs["markdown"] = str(md_path)

        logger.info("TradeLifecycleAudit artifacts saved: %s", list(outputs.values()))
        return outputs

    def _build_markdown(self, report: dict[str, Any]) -> str:
        s1 = report.get("stage1_duration", {})
        s2 = report.get("stage2_exit_reasons", {})
        s3 = report.get("stage3_blocked_time", {})
        s4 = report.get("stage4_exit_quality", {})
        s5 = report.get("stage5_ideal_time", {})
        s6 = report.get("stage6_operational_capacity", {})
        s7 = report.get("stage7_diagnosis", {})
        data = report.get("data", {})

        lines: list[str] = [
            "# Trade Lifecycle Audit — FASE 9.3",
            "",
            f"**Gerado em:** {report.get('generated_at', '')}",
            f"**Execution ID:** {report.get('audit_execution_id', '')}",
            "",
            "---",
            "",
            "## Sumário Executivo",
            "",
            f"- Trades analisados: **{data.get('total_trades', 0)}** "
            f"(fechados: {data.get('closed_trades', 0)}, abertos: {data.get('open_trades', 0)})",
            f"- Sinais registrados: **{data.get('total_signals', 0)}**",
            f"- Timeframe dominante: **{data.get('dominant_timeframe', 'N/A')}**",
            "",
            "---",
            "",
            "## ETAPA 1 — Estatísticas das Posições",
            "",
        ]

        def _row(label: str, stats: dict[str, float], fmt: str = ".2f") -> list[str]:
            return [
                f"**{label}**",
                "",
                f"| Métrica | Valor |",
                f"|---------|-------|",
                f"| Média | {stats.get('mean', 0):{fmt}} |",
                f"| Mediana | {stats.get('median', 0):{fmt}} |",
                f"| Mín | {stats.get('min', 0):{fmt}} |",
                f"| Máx | {stats.get('max', 0):{fmt}} |",
                f"| P50 | {stats.get('p50', 0):{fmt}} |",
                f"| P75 | {stats.get('p75', 0):{fmt}} |",
                f"| P90 | {stats.get('p90', 0):{fmt}} |",
                f"| P95 | {stats.get('p95', 0):{fmt}} |",
                "",
            ]

        lines += _row("Em Candles", s1.get("candles", {}), ".1f")
        lines += _row("Em Minutos", s1.get("minutes", {}), ".1f")
        lines += _row("Em Horas", s1.get("hours", {}), ".2f")

        lines += [
            "---",
            "",
            "## ETAPA 2 — Motivos de Saída",
            "",
            f"Total de trades fechados: **{s2.get('total_closed', 0)}**",
            "",
            "| Motivo | Quantidade | Percentual |",
            "|--------|-----------|------------|",
        ]
        for row in s2.get("breakdown", []):
            lines.append(f"| {row['reason']} | {row['count']} | {row['pct']}% |")
        lines.append("")

        lines += [
            "---",
            "",
            "## ETAPA 3 — Tempo Bloqueado",
            "",
            f"- Sinais BUY totais: **{s3.get('total_buy_signals', 0)}**",
            f"- Entradas aceitas: **{s3.get('accepted_entries', 0)}**",
            f"- Bloqueados por posição aberta: **{s3.get('blocked_by_position', 0)}**",
            f"- Outros rejeitados: **{s3.get('other_rejected', 0)}**",
            f"- Tempo total bloqueado: **{s3.get('total_blocked_minutes', 0):.0f} min** "
            f"({s3.get('blocked_pct_total', 0):.1f}% do período)",
            f"- Média de setups bloqueados por trade: **{s3.get('avg_setups_blocked_per_trade', 0):.2f}**",
            "",
            f"> {s3.get('interpretation', '')}",
            "",
        ]

        lines += [
            "---",
            "",
            "## ETAPA 4 — Qualidade das Saídas (MFE/MAE)",
            "",
            f"- Trades com dados de candle: **{s4.get('trade_count', 0)}**",
            f"- Eficiência média de saída: **{s4.get('exit_efficiency_mean', 0) * 100:.1f}%**",
            f"- MFE médio: **{s4.get('mfe_mean_pct', 0):.2f}%**",
            f"- MAE médio: **{s4.get('mae_mean_pct', 0):.2f}%**",
            f"- Saídas precoces (<50% MFE): **{s4.get('early_exits', 0)}**",
            f"- Saídas tardias (>120% MFE): **{s4.get('late_exits', 0)}**",
            f"- Diagnóstico: **{s4.get('diagnosis_label', 'N/A')}**",
            "",
            f"> {s4.get('interpretation', '')}",
            "",
        ]

        lines += [
            "---",
            "",
            "## ETAPA 5 — Simulações de Tempo Ideal",
            "",
            f"_{s5.get('note', '')}_",
            "",
            "| Cenário | Trades | PnL Total % | PnL Médio % | Win Rate |",
            "|---------|--------|-------------|-------------|----------|",
        ]
        for sc in s5.get("scenarios", []):
            lines.append(
                f"| {sc['description']} | {sc['trade_count']} | "
                f"{sc['total_pnl_pct']:.2f}% | {sc['avg_pnl_pct']:.4f}% | "
                f"{sc['win_rate'] * 100:.1f}% |"
            )
        lines.append("")

        lines += [
            "---",
            "",
            "## ETAPA 6 — Capacidade Operacional",
            "",
            f"- Trades atuais: **{s6.get('current_trades', 0)}**",
            f"- Frequência atual: **{s6.get('freq_per_day', 0):.2f} trades/dia**",
            f"- Duração média: **{s6.get('avg_duration_min', 0):.0f} min** "
            f"({s6.get('avg_duration_candles', 0):.1f} candles)",
            f"- Setups bloqueados (total): **{s6.get('blocked_setups_total', 0)}**",
            "",
            "| Cenário | Dur. Média | Trades Adicionais | Setups Capturados | Nova Frequência | Aumento % |",
            "|---------|-----------|-------------------|-------------------|-----------------|-----------|",
        ]
        for sc in s6.get("scenarios", []):
            lines.append(
                f"| {sc['scenario']} | {sc['new_avg_duration_min']:.0f} min | "
                f"{sc['additional_trades_est']:.1f} | {sc['additional_setups_captured']:.1f} | "
                f"{sc['new_freq_per_day']:.2f}/dia | +{sc['freq_increase_pct']:.1f}% |"
            )
        lines.append("")

        lines += [
            "---",
            "",
            "## ETAPA 7 — Diagnóstico Final",
            "",
            f"### Gargalo Principal: **{s7.get('main_bottleneck', 'N/A').upper()}**",
            "",
            "**Scores de Evidência:**",
            "",
        ]
        for k, v in s7.get("bottleneck_scores", {}).items():
            lines.append(f"- {k}: {v:.1f}")
        lines += [
            "",
            "**Evidências:**",
            "",
        ]
        for ev in s7.get("evidence", []):
            lines.append(f"- {ev}")
        lines += [
            "",
            "### Recomendações (sem alterar a estratégia):",
            "",
        ]
        for i, rec in enumerate(s7.get("recommendation", []), start=1):
            lines.append(f"{i}. {rec}")
        lines += [
            "",
            "---",
            "",
            f"_{s7.get('note', '')}_",
        ]

        return "\n".join(lines) + "\n"

    def _persist_db(
        self,
        audit_id: str,
        cfg: TradeLifecycleAuditConfig,
        report: dict[str, Any],
        started_at: datetime,
    ) -> None:
        try:
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.start_execution_session(
                    execution_id=audit_id,
                    started_at=started_at,
                    status="completed",
                    host=socket.gethostname(),
                    cpu=platform.processor(),
                    workers=1,
                    python_version=platform.python_version(),
                    git_version=os.getenv("GIT_COMMIT"),
                )
                s7 = report.get("stage7_diagnosis", {})
                history.save_checkpoint(
                    execution_id=audit_id,
                    stage="trade_lifecycle_audit",
                    processed=int(report.get("data", {}).get("closed_trades", 0)),
                    completed=True,
                    payload={
                        "main_bottleneck": s7.get("main_bottleneck"),
                        "exit_efficiency_mean": report.get("stage4_exit_quality", {}).get("exit_efficiency_mean"),
                        "blocked_pct_total": report.get("stage3_blocked_time", {}).get("blocked_pct_total"),
                        "total_trades": report.get("data", {}).get("total_trades"),
                        "diagnosis_label": report.get("stage4_exit_quality", {}).get("diagnosis_label"),
                    },
                )
        except Exception as exc:
            logger.warning("TradeLifecycleAudit DB persistence failed (non-fatal): %s", exc)
