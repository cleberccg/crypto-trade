from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
import psutil

from config.settings import BASE_DIR, settings
from database.history_models import (
    BacktestRun,
    ExecutionCheckpoint,
    IndicatorHistorySnapshot,
    OptimizationResultRecord,
    OptimizationRun,
    SignalSnapshot,
    TradeHistory,
    ValidationRun,
)
from database.next_phase_models import ExecutionTimelineEvent
from database.models import Candle
from database.session_models import ExecutionSession, StrategyVersion
from notifications.notification_service import get_notification_service


def _safe_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def get_dashboard_snapshot(session: Session) -> dict:
    open_executions = session.scalar(
        select(func.count()).select_from(ExecutionSession).where(ExecutionSession.status == "running")
    ) or 0
    last_execution = session.execute(
        select(ExecutionSession).order_by(desc(ExecutionSession.created_at)).limit(1)
    ).scalar_one_or_none()
    last_opt = session.execute(select(OptimizationRun).order_by(desc(OptimizationRun.created_at)).limit(1)).scalar_one_or_none()
    result_count = session.scalar(select(func.count()).select_from(OptimizationResultRecord)) or 0
    trade_count = session.scalar(select(func.count()).select_from(TradeHistory)) or 0
    signal_count = session.scalar(select(func.count()).select_from(SignalSnapshot)) or 0
    backtest_count = session.scalar(select(func.count()).select_from(BacktestRun)) or 0
    optimization_count = session.scalar(select(func.count()).select_from(OptimizationRun)) or 0

    best_pf = session.scalar(
        select(func.max(OptimizationResultRecord.profit_factor)).where(OptimizationResultRecord.approved.is_(True))
    )
    avg_sharpe = session.scalar(select(func.avg(OptimizationResultRecord.sharpe)))
    avg_expectancy = session.scalar(select(func.avg(OptimizationResultRecord.expectancy)))
    max_drawdown = session.scalar(select(func.max(OptimizationResultRecord.drawdown)))

    latest_backtest = session.execute(
        select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(1)
    ).scalar_one_or_none()

    capital_initial = float(latest_backtest.initial_capital) if latest_backtest else None
    capital_current = float(latest_backtest.final_capital) if latest_backtest and latest_backtest.final_capital is not None else None

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    daily_profit = session.scalar(
        select(func.coalesce(func.sum(TradeHistory.pnl), 0)).where(TradeHistory.exit_time >= day_start)
    ) or 0.0
    weekly_profit = session.scalar(
        select(func.coalesce(func.sum(TradeHistory.pnl), 0)).where(TradeHistory.exit_time >= week_start)
    ) or 0.0
    monthly_profit = session.scalar(
        select(func.coalesce(func.sum(TradeHistory.pnl), 0)).where(TradeHistory.exit_time >= month_start)
    ) or 0.0

    cpu_percent = psutil.cpu_percent(interval=0.05)
    ram_percent = psutil.virtual_memory().percent

    running_session = session.execute(
        select(ExecutionSession)
        .where(ExecutionSession.status == "running")
        .order_by(ExecutionSession.started_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    runtime_seconds = None
    if running_session and running_session.started_at:
        runtime_seconds = max(0.0, (now - running_session.started_at.astimezone(timezone.utc)).total_seconds())

    return {
        "system_status": "ONLINE" if open_executions > 0 else "OFFLINE",
        "mode": settings.trading.mode.upper(),
        "active_strategy": settings.trading.strategy,
        "symbol": settings.trading.default_symbol,
        "timeframe": settings.trading.default_timeframe,
        "capital_initial": capital_initial,
        "capital_current": capital_current,
        "daily_profit": float(daily_profit),
        "weekly_profit": float(weekly_profit),
        "monthly_profit": float(monthly_profit),
        "running_executions": open_executions,
        "last_execution_id": last_execution.execution_id if last_execution else None,
        "last_execution_status": last_execution.status if last_execution else None,
        "last_execution_at": _safe_dt(last_execution.created_at) if last_execution else None,
        "last_optimization_id": last_opt.execution_id if last_opt else None,
        "last_optimization_status": last_opt.status if last_opt else None,
        "best_profit_factor": float(best_pf) if best_pf is not None else None,
        "avg_sharpe": float(avg_sharpe) if avg_sharpe is not None else None,
        "avg_expectancy": float(avg_expectancy) if avg_expectancy is not None else None,
        "max_drawdown": float(max_drawdown) if max_drawdown is not None else None,
        "trade_count": trade_count,
        "signal_count": signal_count,
        "backtest_count": backtest_count,
        "optimization_count": optimization_count,
        "optimization_results_count": result_count,
        "cpu": cpu_percent,
        "ram": ram_percent,
        "runtime_seconds": runtime_seconds,
        "updated_at": _safe_dt(datetime.now(timezone.utc)),
    }


def paginate_query(base_query, page: int, page_size: int, session: Session) -> tuple[list, int]:
    total = session.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    rows = session.execute(
        base_query.offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return rows, total


def list_execution_sessions(
    session: Session,
    page: int,
    page_size: int,
    status: str | None,
    execution_type: str | None,
) -> tuple[list[dict], int]:
    query = select(ExecutionSession).order_by(desc(ExecutionSession.created_at))
    if status:
        query = query.where(ExecutionSession.status == status)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "execution_id": row.execution_id,
                "status": row.status,
                "started_at": _safe_dt(row.started_at),
                "finished_at": _safe_dt(row.finished_at),
                "duration": row.duration,
                "workers": row.workers,
                "host": row.host,
                "cpu": row.cpu,
                "python_version": row.python_version,
                "type": execution_type or "optimization",
            }
        )
    return items, total


def list_optimizations(
    session: Session,
    page: int,
    page_size: int,
    symbol: str | None,
    timeframe: str | None,
    status: str | None,
) -> tuple[list[dict], int]:
    query = select(OptimizationRun).order_by(desc(OptimizationRun.created_at))
    if symbol:
        query = query.where(OptimizationRun.symbol == symbol)
    if timeframe:
        query = query.where(OptimizationRun.timeframe == timeframe)
    if status:
        query = query.where(OptimizationRun.status == status)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        processed = session.scalar(
            select(func.count()).select_from(OptimizationResultRecord).where(
                OptimizationResultRecord.execution_id == row.execution_id
            )
        ) or 0
        remaining = max(0, row.total_combinations - processed)
        items.append(
            {
                "execution_id": row.execution_id,
                "strategy": row.strategy,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "status": row.status,
                "workers": row.workers,
                "started_at": _safe_dt(row.started_at),
                "finished_at": _safe_dt(row.finished_at),
                "duration_seconds": row.duration_seconds,
                "total_combinations": row.total_combinations,
                "processed_combinations": processed,
                "remaining_combinations": remaining,
            }
        )
    return items, total


def optimization_top_results(session: Session, execution_id: str, limit: int = 100) -> list[dict]:
    rows = session.execute(
        select(OptimizationResultRecord)
        .where(OptimizationResultRecord.execution_id == execution_id)
        .order_by(desc(OptimizationResultRecord.profit_factor), desc(OptimizationResultRecord.net_profit))
        .limit(limit)
    ).scalars().all()
    payload = []
    for row in rows:
        payload.append(
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "profit_factor": row.profit_factor,
                "net_profit": row.net_profit,
                "drawdown": row.drawdown,
                "win_rate": row.win_rate,
                "sharpe": row.sharpe,
                "expectancy": row.expectancy,
                "approved": row.approved,
                "parameters_json": row.parameters_json,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return payload


def list_backtests(
    session: Session,
    page: int,
    page_size: int,
    strategy: str | None,
    symbol: str | None,
) -> tuple[list[dict], int]:
    query = select(BacktestRun).order_by(desc(BacktestRun.created_at))
    if strategy:
        query = query.where(BacktestRun.strategy == strategy)
    if symbol:
        query = query.where(BacktestRun.symbol == symbol)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "execution_id": row.execution_id,
                "strategy": row.strategy,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "status": row.status,
                "start_date": _safe_dt(row.start_date),
                "end_date": _safe_dt(row.end_date),
                "initial_capital": row.initial_capital,
                "final_capital": row.final_capital,
                "total_trades": row.total_trades,
                "win_rate": row.win_rate,
                "profit_factor": row.profit_factor,
                "sharpe": row.sharpe,
                "expectancy": row.expectancy,
                "drawdown": row.drawdown,
            }
        )
    return items, total


def list_trades(
    session: Session,
    page: int,
    page_size: int,
    symbol: str | None,
    strategy: str | None,
    min_pnl: float | None,
    max_pnl: float | None,
) -> tuple[list[dict], int]:
    query = select(TradeHistory).order_by(desc(TradeHistory.created_at))
    if symbol:
        query = query.where(TradeHistory.symbol == symbol)
    if strategy:
        query = query.where(TradeHistory.strategy == strategy)
    if min_pnl is not None:
        query = query.where(TradeHistory.pnl >= min_pnl)
    if max_pnl is not None:
        query = query.where(TradeHistory.pnl <= max_pnl)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "strategy": row.strategy,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "side": row.side,
                "entry_time": _safe_dt(row.entry_time),
                "exit_time": _safe_dt(row.exit_time),
                "entry_price": row.entry_price,
                "exit_price": row.exit_price,
                "stop_loss": row.stop_loss,
                "take_profit": row.take_profit,
                "risk_reward": row.risk_reward,
                "pnl": row.pnl,
                "duration_minutes": row.duration_minutes,
                "exit_reason": row.exit_reason,
                "score": row.score,
            }
        )
    return items, total


def list_signals(
    session: Session,
    page: int,
    page_size: int,
    symbol: str | None,
    strategy: str | None,
    accepted: bool | None,
    signal_type: str | None,
) -> tuple[list[dict], int]:
    query = select(SignalSnapshot).order_by(desc(SignalSnapshot.created_at))
    if symbol:
        query = query.where(SignalSnapshot.symbol == symbol)
    if strategy:
        query = query.where(SignalSnapshot.strategy == strategy)
    if accepted is not None:
        query = query.where(SignalSnapshot.accepted.is_(accepted))
    if signal_type:
        query = query.where(SignalSnapshot.signal == signal_type)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "strategy": row.strategy,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "timestamp": _safe_dt(row.timestamp),
                "signal": row.signal,
                "score": row.score,
                "entry_price": row.entry_price,
                "stop_loss": row.stop_loss,
                "take_profit": row.take_profit,
                "rr": row.rr,
                "accepted": row.accepted,
                "rejection_reason": row.rejection_reason,
                "market_regime": row.market_regime,
            }
        )
    return items, total


def get_analytics_snapshot(session: Session) -> dict:
    pf_by_symbol = session.execute(
        select(OptimizationResultRecord.symbol, func.avg(OptimizationResultRecord.profit_factor))
        .group_by(OptimizationResultRecord.symbol)
        .order_by(desc(func.avg(OptimizationResultRecord.profit_factor)))
    ).all()

    pf_by_timeframe = session.execute(
        select(OptimizationResultRecord.timeframe, func.avg(OptimizationResultRecord.profit_factor))
        .group_by(OptimizationResultRecord.timeframe)
        .order_by(desc(func.avg(OptimizationResultRecord.profit_factor)))
    ).all()

    win_rate_avg = session.scalar(select(func.avg(OptimizationResultRecord.win_rate)))
    drawdown_avg = session.scalar(select(func.avg(OptimizationResultRecord.drawdown)))
    sharpe_avg = session.scalar(select(func.avg(OptimizationResultRecord.sharpe)))
    expectancy_avg = session.scalar(select(func.avg(OptimizationResultRecord.expectancy)))

    ema_fast_best = session.execute(
        select(OptimizationResultRecord.ema_fast, func.avg(OptimizationResultRecord.net_profit))
        .group_by(OptimizationResultRecord.ema_fast)
        .order_by(desc(func.avg(OptimizationResultRecord.net_profit)))
        .limit(10)
    ).all()

    rr_best = session.execute(
        select(OptimizationResultRecord.risk_reward_ratio, func.avg(OptimizationResultRecord.net_profit))
        .group_by(OptimizationResultRecord.risk_reward_ratio)
        .order_by(desc(func.avg(OptimizationResultRecord.net_profit)))
        .limit(10)
    ).all()

    return {
        "profit_factor_by_symbol": [{"symbol": r[0], "value": float(r[1]) if r[1] is not None else None} for r in pf_by_symbol],
        "profit_factor_by_timeframe": [{"timeframe": r[0], "value": float(r[1]) if r[1] is not None else None} for r in pf_by_timeframe],
        "win_rate_avg": float(win_rate_avg) if win_rate_avg is not None else None,
        "drawdown_avg": float(drawdown_avg) if drawdown_avg is not None else None,
        "sharpe_avg": float(sharpe_avg) if sharpe_avg is not None else None,
        "expectancy_avg": float(expectancy_avg) if expectancy_avg is not None else None,
        "best_ema_fast": [{"ema_fast": r[0], "avg_net_profit": float(r[1]) if r[1] is not None else None} for r in ema_fast_best],
        "best_risk_reward": [{"risk_reward_ratio": r[0], "avg_net_profit": float(r[1]) if r[1] is not None else None} for r in rr_best],
    }


def list_validation_runs(session: Session, page: int, page_size: int) -> tuple[list[dict], int]:
    query = select(ValidationRun).order_by(desc(ValidationRun.created_at))
    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "execution_id": row.execution_id,
                "optimizer_run": row.optimizer_run,
                "total_tested": row.total_tested,
                "approved": row.approved,
                "rejected": row.rejected,
                "min_profit_factor": row.min_profit_factor,
                "min_trades": row.min_trades,
                "max_drawdown": row.max_drawdown,
                "validation_status": row.validation_status,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return items, total


def list_checkpoints(session: Session, execution_id: str | None, page: int, page_size: int) -> tuple[list[dict], int]:
    query = select(ExecutionCheckpoint).order_by(desc(ExecutionCheckpoint.created_at))
    if execution_id:
        query = query.where(ExecutionCheckpoint.execution_id == execution_id)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "stage": row.stage,
                "processed": row.processed,
                "completed": row.completed,
                "payload_json": row.payload_json,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return items, total


def list_strategy_versions(session: Session, page: int, page_size: int) -> tuple[list[dict], int]:
    query = select(StrategyVersion).order_by(desc(StrategyVersion.created_at))
    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "strategy_name": row.strategy_name,
                "version": row.version,
                "git_commit": row.git_commit,
                "description": row.description,
                "active": row.active,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return items, total


def list_db_tables_snapshot(session: Session, page: int, page_size: int, table_name: str | None) -> tuple[list[dict], int]:
    table_selectors = {
        "optimization_runs": lambda: select(OptimizationRun).order_by(desc(OptimizationRun.created_at)),
        "optimization_results_history": lambda: select(OptimizationResultRecord).order_by(desc(OptimizationResultRecord.created_at)),
        "backtest_runs": lambda: select(BacktestRun).order_by(desc(BacktestRun.created_at)),
        "trade_history": lambda: select(TradeHistory).order_by(desc(TradeHistory.created_at)),
        "signal_snapshots": lambda: select(SignalSnapshot).order_by(desc(SignalSnapshot.created_at)),
        "indicator_snapshots": lambda: select(IndicatorHistorySnapshot).order_by(desc(IndicatorHistorySnapshot.created_at)),
        "validation_runs": lambda: select(ValidationRun).order_by(desc(ValidationRun.created_at)),
        "execution_sessions": lambda: select(ExecutionSession).order_by(desc(ExecutionSession.created_at)),
        "execution_checkpoints": lambda: select(ExecutionCheckpoint).order_by(desc(ExecutionCheckpoint.created_at)),
        "candles": lambda: select(Candle).order_by(desc(Candle.created_at)),
    }

    if table_name and table_name in table_selectors:
        query = table_selectors[table_name]()
        rows, total = paginate_query(query, page, page_size, session)
        items = [{"table": table_name, "row": str(row)} for row in rows]
        return items, total

    totals = []
    for key, selector in table_selectors.items():
        count = session.scalar(select(func.count()).select_from(selector().subquery())) or 0
        totals.append({"table": key, "rows": int(count)})

    total = len(totals)
    start = (page - 1) * page_size
    end = start + page_size
    return totals[start:end], total


def read_logs(level: str | None, q: str | None, max_lines: int = 400) -> list[dict]:
    log_dir = settings.logging.log_dir
    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []

    selected: list[dict] = []
    for file in files:
        lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in reversed(lines):
            if level and f" {level.upper()} " not in line:
                continue
            if q and q.lower() not in line.lower():
                continue
            selected.append({"file": file.name, "line": line})
            if len(selected) >= max_lines:
                return selected
    return selected


def read_logs_as_text(level: str | None, q: str | None, max_lines: int = 1000) -> str:
    entries = read_logs(level=level, q=q, max_lines=max_lines)
    return "\n".join(f"[{entry['file']}] {entry['line']}" for entry in entries)


def stream_tail_lines(path: Path, limit: int = 60) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]


def get_execution_details(session: Session, execution_id: str) -> dict | None:
    execution = session.execute(
        select(ExecutionSession).where(ExecutionSession.execution_id == execution_id)
    ).scalar_one_or_none()
    if execution is None:
        return None

    optimization = session.execute(
        select(OptimizationRun).where(OptimizationRun.execution_id == execution_id)
    ).scalar_one_or_none()

    checkpoints = session.execute(
        select(ExecutionCheckpoint)
        .where(ExecutionCheckpoint.execution_id == execution_id)
        .order_by(desc(ExecutionCheckpoint.created_at))
        .limit(30)
    ).scalars().all()

    return {
        "execution": {
            "execution_id": execution.execution_id,
            "status": execution.status,
            "started_at": _safe_dt(execution.started_at),
            "finished_at": _safe_dt(execution.finished_at),
            "duration": execution.duration,
            "host": execution.host,
            "cpu": execution.cpu,
            "workers": execution.workers,
            "python_version": execution.python_version,
            "git_version": execution.git_version,
        },
        "optimization": {
            "strategy": optimization.strategy if optimization else None,
            "symbol": optimization.symbol if optimization else None,
            "timeframe": optimization.timeframe if optimization else None,
            "total_combinations": optimization.total_combinations if optimization else None,
            "status": optimization.status if optimization else None,
        },
        "recent_checkpoints": [
            {
                "id": item.id,
                "stage": item.stage,
                "processed": item.processed,
                "completed": item.completed,
                "created_at": _safe_dt(item.created_at),
            }
            for item in checkpoints
        ],
    }


def get_backtest_details(session: Session, execution_id: str) -> dict | None:
    backtest = session.execute(
        select(BacktestRun).where(BacktestRun.execution_id == execution_id)
    ).scalar_one_or_none()
    if backtest is None:
        return None

    trades = session.execute(
        select(TradeHistory)
        .where(TradeHistory.execution_id == execution_id)
        .order_by(TradeHistory.exit_time.asc(), TradeHistory.id.asc())
        .limit(1000)
    ).scalars().all()

    equity_curve: list[dict] = []
    running_equity = float(backtest.initial_capital)
    peak = running_equity
    for idx, trade in enumerate(trades, start=1):
        running_equity += float(trade.pnl or 0.0)
        peak = max(peak, running_equity)
        drawdown_pct = ((peak - running_equity) / peak * 100.0) if peak > 0 else 0.0
        equity_curve.append(
            {
                "step": idx,
                "time": _safe_dt(trade.exit_time or trade.entry_time),
                "equity": running_equity,
                "drawdown_pct": drawdown_pct,
                "pnl": float(trade.pnl or 0.0),
            }
        )

    return {
        "backtest": {
            "execution_id": backtest.execution_id,
            "strategy": backtest.strategy,
            "symbol": backtest.symbol,
            "timeframe": backtest.timeframe,
            "start_date": _safe_dt(backtest.start_date),
            "end_date": _safe_dt(backtest.end_date),
            "initial_capital": float(backtest.initial_capital),
            "final_capital": float(backtest.final_capital) if backtest.final_capital is not None else None,
            "total_trades": backtest.total_trades,
            "win_rate": backtest.win_rate,
            "profit_factor": backtest.profit_factor,
            "sharpe": backtest.sharpe,
            "expectancy": backtest.expectancy,
            "drawdown": backtest.drawdown,
            "status": backtest.status,
        },
        "equity_curve": equity_curve,
        "trades": [
            {
                "id": item.id,
                "side": item.side,
                "entry_time": _safe_dt(item.entry_time),
                "exit_time": _safe_dt(item.exit_time),
                "entry_price": item.entry_price,
                "exit_price": item.exit_price,
                "pnl": item.pnl,
                "risk_reward": item.risk_reward,
                "duration_minutes": item.duration_minutes,
                "exit_reason": item.exit_reason,
                "score": item.score,
            }
            for item in trades
        ],
    }


def get_monitor_snapshot(session: Session) -> dict:
    latest_signal = session.execute(select(SignalSnapshot).order_by(desc(SignalSnapshot.created_at)).limit(1)).scalar_one_or_none()
    open_positions = session.scalar(
        select(func.count()).select_from(TradeHistory).where(TradeHistory.exit_time.is_(None))
    ) or 0
    current_pnl = session.scalar(select(func.sum(TradeHistory.pnl))) or 0.0

    latest_candle = session.execute(
        select(Candle)
        .where(Candle.symbol == settings.trading.default_symbol, Candle.timeframe == settings.trading.default_timeframe)
        .order_by(desc(Candle.open_time))
        .limit(1)
    ).scalar_one_or_none()

    return {
        "price": float(latest_candle.close) if latest_candle else None,
        "open_positions": int(open_positions),
        "current_pnl": float(current_pnl),
        "capital": None,
        "last_signal": latest_signal.signal if latest_signal else None,
        "bot_status": "ONLINE" if open_positions > 0 else "IDLE",
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": psutil.virtual_memory().percent,
        "uptime": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_observability_snapshot(session: Session) -> dict:
    now = datetime.now(timezone.utc)
    running_executions = session.scalar(
        select(func.count()).select_from(ExecutionSession).where(ExecutionSession.status == "running")
    ) or 0

    last_checkpoint = session.execute(
        select(ExecutionCheckpoint)
        .order_by(desc(ExecutionCheckpoint.created_at))
        .limit(1)
    ).scalar_one_or_none()

    last_optimization = session.execute(
        select(OptimizationRun)
        .order_by(desc(OptimizationRun.created_at))
        .limit(1)
    ).scalar_one_or_none()

    processed_combinations = 0
    remaining_combinations = 0
    if last_optimization is not None:
        processed_combinations = session.scalar(
            select(func.count())
            .select_from(OptimizationResultRecord)
            .where(OptimizationResultRecord.execution_id == last_optimization.execution_id)
        ) or 0
        remaining_combinations = max(0, int(last_optimization.total_combinations) - int(processed_combinations))

    latest_sessions = session.execute(
        select(ExecutionSession)
        .order_by(desc(ExecutionSession.created_at))
        .limit(8)
    ).scalars().all()

    sessions_payload: list[dict] = []
    for row in latest_sessions:
        sessions_payload.append(
            {
                "execution_id": row.execution_id,
                "status": row.status,
                "started_at": _safe_dt(row.started_at),
                "finished_at": _safe_dt(row.finished_at),
                "duration": row.duration,
                "workers": row.workers,
            }
        )

    cpu = psutil.cpu_percent(interval=0.05)
    ram = psutil.virtual_memory().percent

    checkpoint_age_seconds = None
    if last_checkpoint is not None and last_checkpoint.created_at is not None:
        checkpoint_age_seconds = max(
            0.0,
            (now - last_checkpoint.created_at.astimezone(timezone.utc)).total_seconds(),
        )

    telegram = get_notification_service().telemetry()

    return {
        "system_time": now.isoformat(),
        "running_executions": int(running_executions),
        "last_checkpoint": {
            "execution_id": last_checkpoint.execution_id if last_checkpoint else None,
            "stage": last_checkpoint.stage if last_checkpoint else None,
            "processed": last_checkpoint.processed if last_checkpoint else None,
            "completed": last_checkpoint.completed if last_checkpoint else None,
            "created_at": _safe_dt(last_checkpoint.created_at) if last_checkpoint else None,
            "age_seconds": checkpoint_age_seconds,
        },
        "last_optimization": {
            "execution_id": last_optimization.execution_id if last_optimization else None,
            "status": last_optimization.status if last_optimization else None,
            "symbol": last_optimization.symbol if last_optimization else None,
            "timeframe": last_optimization.timeframe if last_optimization else None,
            "strategy": last_optimization.strategy if last_optimization else None,
            "total_combinations": last_optimization.total_combinations if last_optimization else None,
            "processed_combinations": int(processed_combinations),
            "remaining_combinations": int(remaining_combinations),
            "started_at": _safe_dt(last_optimization.started_at) if last_optimization else None,
            "finished_at": _safe_dt(last_optimization.finished_at) if last_optimization else None,
        },
        "host": {
            "cpu_percent": cpu,
            "ram_percent": ram,
        },
        "telegram": telegram,
        "recent_sessions": sessions_payload,
    }


def list_execution_timeline_events(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ExecutionTimelineEvent)
        .order_by(desc(ExecutionTimelineEvent.created_at), desc(ExecutionTimelineEvent.id))
        .limit(limit)
    ).scalars().all()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "event_type": row.event_type,
                "title": row.title,
                "details": row.details,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return items


def list_indicators(
    session: Session,
    page: int,
    page_size: int,
    min_rsi: float | None,
    max_rsi: float | None,
) -> tuple[list[dict], int]:
    query = select(IndicatorHistorySnapshot).order_by(desc(IndicatorHistorySnapshot.created_at))
    if min_rsi is not None:
        query = query.where(IndicatorHistorySnapshot.rsi >= min_rsi)
    if max_rsi is not None:
        query = query.where(IndicatorHistorySnapshot.rsi <= max_rsi)

    rows, total = paginate_query(query, page, page_size, session)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "signal_id": row.signal_id,
                "ema_fast": row.ema_fast,
                "ema_slow": row.ema_slow,
                "ema_trend": row.ema_trend,
                "rsi": row.rsi,
                "atr": row.atr,
                "volume": row.volume,
                "volume_average": row.volume_average,
                "close": row.close,
                "high": row.high,
                "low": row.low,
                "created_at": _safe_dt(row.created_at),
            }
        )
    return items, total


def current_config_payload() -> dict:
    return {
        "mode": settings.trading.mode,
        "symbol": settings.trading.default_symbol,
        "timeframe": settings.trading.default_timeframe,
        "workers": settings.optimizer.workers,
        "strategy": settings.trading.strategy,
        "database_url": settings.database.url,
    }


_runtime_overrides: dict[str, str | int] = {}


def _persist_env_updates(updates: dict[str, str]) -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        lines: list[str] = []
    else:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()

    key_to_idx: dict[str, int] = {}
    for idx, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        key_to_idx[key] = idx

    for key, value in updates.items():
        serialized = f"{key}={value}"
        if key in key_to_idx:
            lines[key_to_idx[key]] = serialized
        else:
            lines.append(serialized)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_runtime_config(payload: dict[str, str | int]) -> dict:
    # Keep overrides in-memory only; do not mutate frozen settings dataclasses.
    allowed = {"mode", "symbol", "timeframe", "workers"}
    for key, value in payload.items():
        if key in allowed:
            _runtime_overrides[key] = value

    base = current_config_payload()
    if "mode" in _runtime_overrides:
        base["mode"] = str(_runtime_overrides["mode"]).lower()
    if "symbol" in _runtime_overrides:
        base["symbol"] = str(_runtime_overrides["symbol"])
    if "timeframe" in _runtime_overrides:
        base["timeframe"] = str(_runtime_overrides["timeframe"])
    if "workers" in _runtime_overrides:
        base["workers"] = int(_runtime_overrides["workers"])

    updates: dict[str, str] = {}
    if "mode" in payload:
        updates["MODE"] = str(payload["mode"]).lower()
    if "symbol" in payload:
        updates["DEFAULT_SYMBOL"] = str(payload["symbol"])
    if "timeframe" in payload:
        updates["DEFAULT_TIMEFRAME"] = str(payload["timeframe"])
    if "workers" in payload:
        updates["OPTIMIZER_WORKERS"] = str(int(payload["workers"]))
    if updates:
        _persist_env_updates(updates)

    return base
