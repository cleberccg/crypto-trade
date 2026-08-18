from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from database.connection import get_session
from database.history_models import ScientificTradeSnapshot, TradeHistory
from sqlalchemy import func, or_, select
from utils.metrics import (
    expectancy_from_pnl,
    max_drawdown_from_pnl,
    profit_factor_from_pnl,
    sharpe_from_pnl,
    win_rate_from_pnl,
)


@dataclass(frozen=True)
class StateRow:
    path: Path
    execution_id: str
    strategy_name: str
    strategy_version: str
    campaign_id: str
    symbol: str
    timeframe: str
    cycles: int
    updated_at: datetime | None
    open_trade_qty: float
    open_trade_symbol: str
    broker_available_qty: float

    @property
    def lag_seconds(self) -> float | None:
        if self.updated_at is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - self.updated_at).total_seconds())

    @property
    def desync(self) -> bool:
        if self.open_trade_qty <= 0:
            return False
        return self.broker_available_qty + 1e-12 < self.open_trade_qty


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_state(path: Path) -> StateRow | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    runtime_state = payload.get("runtime_state") if isinstance(payload.get("runtime_state"), dict) else {}
    open_trade = runtime_state.get("open_trade") if isinstance(runtime_state.get("open_trade"), dict) else {}
    broker = runtime_state.get("broker") if isinstance(runtime_state.get("broker"), dict) else {}
    positions = broker.get("positions") if isinstance(broker.get("positions"), dict) else {}

    symbol = str(payload.get("symbol") or "")
    open_trade_symbol = str(open_trade.get("symbol") or symbol)
    base_asset = open_trade_symbol.split("/")[0] if "/" in open_trade_symbol else open_trade_symbol

    open_trade_qty = 0.0
    try:
        open_trade_qty = float(open_trade.get("quantity") or 0.0)
    except (TypeError, ValueError):
        open_trade_qty = 0.0

    broker_available_qty = 0.0
    try:
        broker_available_qty = float(positions.get(base_asset, 0.0))
    except (TypeError, ValueError):
        broker_available_qty = 0.0

    try:
        cycles = int(payload.get("cycles") or 0)
    except (TypeError, ValueError):
        cycles = 0

    return StateRow(
        path=path,
        execution_id=str(payload.get("execution_id") or ""),
        strategy_name=str(payload.get("strategy_name") or ""),
        strategy_version=str(payload.get("strategy_version") or ""),
        campaign_id=str(payload.get("campaign_id") or ""),
        symbol=symbol,
        timeframe=str(payload.get("timeframe") or ""),
        cycles=cycles,
        updated_at=parse_dt(payload.get("updated_at")),
        open_trade_qty=open_trade_qty,
        open_trade_symbol=open_trade_symbol,
        broker_available_qty=broker_available_qty,
    )


def infer_current_campaign(rows: list[StateRow]) -> str:
    by_campaign: dict[str, datetime] = {}
    for row in rows:
        if not row.campaign_id or row.updated_at is None:
            continue
        prev = by_campaign.get(row.campaign_id)
        if prev is None or row.updated_at > prev:
            by_campaign[row.campaign_id] = row.updated_at
    if not by_campaign:
        return ""
    return sorted(by_campaign.items(), key=lambda kv: kv[1], reverse=True)[0][0]


def latest_file(pattern: str, results_dir: Path) -> Path | None:
    files = list(results_dir.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def safe_read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    return f"{hours:.2f}h"


def fetch_closed_trades_metrics(execution_ids: list[str]) -> dict[str, float | int]:
    ids = sorted({x.strip() for x in execution_ids if str(x).strip()})
    if not ids:
        return {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "net_profit": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
        }

    pnl_values: list[float] = []
    pnl_percent_values: list[float] = []

    with get_session() as session:
        stmt = (
            select(
                TradeHistory.pnl,
                TradeHistory.pnl_percent,
                TradeHistory.exit_time,
            )
            .where(TradeHistory.execution_id.in_(ids))
            .where(TradeHistory.exit_time.is_not(None))
        )
        rows = session.execute(stmt).all()

    for pnl, pnl_percent, _exit_time in rows:
        try:
            pnl_values.append(float(pnl or 0.0))
        except (TypeError, ValueError):
            pnl_values.append(0.0)
        try:
            pnl_percent_values.append(float(pnl_percent or 0.0))
        except (TypeError, ValueError):
            pnl_percent_values.append(0.0)

    closed_trades = len(pnl_values)
    wins = sum(1 for x in pnl_values if x > 0)
    losses = sum(1 for x in pnl_values if x < 0)

    sharpe_base = pnl_percent_values if any(abs(x) > 0 for x in pnl_percent_values) else pnl_values

    return {
        "closed_trades": int(closed_trades),
        "wins": int(wins),
        "losses": int(losses),
        "net_profit": float(sum(pnl_values)),
        "win_rate": float(win_rate_from_pnl(pnl_values)),
        "profit_factor": float(profit_factor_from_pnl(pnl_values)),
        "expectancy": float(expectancy_from_pnl(pnl_values)),
        "max_drawdown": float(max_drawdown_from_pnl(pnl_values)),
        "sharpe": float(sharpe_from_pnl(sharpe_base)),
    }


def fetch_scientific_trade_metrics(
    strategy_names: list[str],
    strategy_versions: list[str] | None = None,
) -> tuple[int, datetime | None, str]:
    names = sorted({str(x).strip() for x in strategy_names if str(x).strip()})
    versions = sorted({str(x).strip() for x in (strategy_versions or []) if str(x).strip()})
    latest_entry_time: datetime | None = None
    reason = "no strategy filter"

    if not names and not versions:
        return 0, latest_entry_time, reason

    exact_names = [n for n in names if "@" in n]
    base_names = [n for n in names if "@" not in n]
    for version in versions:
        suffix = f"@{version}"
        if not any(name.endswith(suffix) for name in names):
            base_names.append(version)

    filter_clauses = []
    if exact_names:
        filter_clauses.append(ScientificTradeSnapshot.strategy.in_(exact_names))
    for name in base_names:
        filter_clauses.append(ScientificTradeSnapshot.strategy.like(f"{name}%"))

    if not filter_clauses:
        return 0, latest_entry_time, reason

    reason = (
        f"strategy={','.join(names) or 'n/a'}; versions={','.join(versions) or 'n/a'}"
        if names or versions
        else "strategy filter not provided"
    )

    with get_session() as session:
        count_value = session.execute(
            select(func.count(ScientificTradeSnapshot.id)).where(or_(*filter_clauses))
        ).scalar_one() or 0

        latest_row = session.execute(
            select(ScientificTradeSnapshot.entry_timestamp)
            .where(or_(*filter_clauses))
            .order_by(ScientificTradeSnapshot.entry_timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()

    if latest_row is not None:
        latest_entry_time = parse_dt(latest_row)

    return int(count_value), latest_entry_time, reason


def fetch_database_trade_metrics(
    strategy_names: list[str],
    strategy_versions: list[str] | None = None,
) -> tuple[dict[str, float | int], datetime | None, datetime | None, datetime | None]:
    names = sorted({str(x).strip() for x in strategy_names if str(x).strip()})
    versions = sorted({str(x).strip() for x in (strategy_versions or []) if str(x).strip()})
    metrics: dict[str, float | int] = {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "net_profit": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "expectancy": 0.0,
        "max_drawdown": 0.0,
        "sharpe": 0.0,
    }
    latest_trade_time: datetime | None = None
    latest_entry_time: datetime | None = None
    latest_exit_time: datetime | None = None

    if not names and not versions:
        return metrics, latest_trade_time, latest_entry_time, latest_exit_time

    exact_names = [n for n in names if "@" in n]
    base_names = [n for n in names if "@" not in n]
    for version in versions:
        suffix = f"@{version}"
        if not any(name.endswith(suffix) for name in names):
            base_names.append(version)

    filter_clauses = []
    if exact_names:
        filter_clauses.append(TradeHistory.strategy.in_(exact_names))
    for name in base_names:
        filter_clauses.append(TradeHistory.strategy.like(f"{name}%"))

    if not filter_clauses:
        return metrics, latest_trade_time, latest_entry_time, latest_exit_time

    with get_session() as session:
        stmt = (
            select(
                TradeHistory.pnl,
                TradeHistory.pnl_percent,
                TradeHistory.entry_time,
                TradeHistory.exit_time,
            )
            .where(or_(*filter_clauses))
            .where(TradeHistory.exit_time.is_not(None))
        )
        rows = session.execute(stmt).all()

    pnl_values: list[float] = []
    pnl_percent_values: list[float] = []
    if rows:
        for pnl, pnl_percent, entry_time, exit_time in rows:
            try:
                pnl_values.append(float(pnl or 0.0))
            except (TypeError, ValueError):
                pnl_values.append(0.0)
            try:
                pnl_percent_values.append(float(pnl_percent or 0.0))
            except (TypeError, ValueError):
                pnl_percent_values.append(0.0)
            parsed_entry = parse_dt(entry_time)
            if parsed_entry is not None and (latest_entry_time is None or parsed_entry > latest_entry_time):
                latest_entry_time = parsed_entry
            if exit_time is not None:
                parsed_exit = parse_dt(exit_time)
                if parsed_exit is not None and (latest_exit_time is None or parsed_exit > latest_exit_time):
                    latest_exit_time = parsed_exit
                if latest_trade_time is None or parsed_exit > latest_trade_time:
                    latest_trade_time = parsed_exit

    closed_trades = len(pnl_values)
    wins = sum(1 for x in pnl_values if x > 0)
    losses = sum(1 for x in pnl_values if x < 0)
    sharpe_base = pnl_percent_values if any(abs(x) > 0 for x in pnl_percent_values) else pnl_values

    metrics.update(
        {
            "closed_trades": int(closed_trades),
            "wins": int(wins),
            "losses": int(losses),
            "net_profit": float(sum(pnl_values)),
            "win_rate": float(win_rate_from_pnl(pnl_values)),
            "profit_factor": float(profit_factor_from_pnl(pnl_values)),
            "expectancy": float(expectancy_from_pnl(pnl_values)),
            "max_drawdown": float(max_drawdown_from_pnl(pnl_values)),
            "sharpe": float(sharpe_from_pnl(sharpe_base)),
        }
    )
    return metrics, latest_trade_time, latest_entry_time, latest_exit_time


def main() -> int:
    parser = argparse.ArgumentParser(description="Status do processo paper-live/supervisor.")
    parser.add_argument("--results-dir", default="optimization/results", help="Diretorio de resultados.")
    parser.add_argument("--campaign-id", default="", help="Filtra por campaign_id. Se vazio, usa a campanha mais recente.")
    parser.add_argument("--max-stale-min", type=float, default=5.0, help="Limite para considerar contexto stale.")
    parser.add_argument("--show-contexts", action="store_true", help="Mostra todos os contextos com lag e ciclos.")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"[ERRO] diretorio nao encontrado: {results_dir}")
        return 2

    rows = [row for row in (load_state(p) for p in sorted(results_dir.glob("paper_live_state__*.json"))) if row is not None]
    if not rows:
        print("[ERRO] nenhum state file encontrado (paper_live_state__*.json)")
        return 2

    campaign_id = str(args.campaign_id or "").strip() or infer_current_campaign(rows)
    if campaign_id:
        rows = [r for r in rows if r.campaign_id == campaign_id]

    if not rows:
        print("[ERRO] nenhum contexto encontrado para o filtro de campanha informado")
        return 2

    stale_limit_seconds = max(1.0, float(args.max_stale_min)) * 60.0
    lags = [r.lag_seconds for r in rows if r.lag_seconds is not None]

    stale_rows = [r for r in rows if r.lag_seconds is None or r.lag_seconds > stale_limit_seconds]
    active_rows = [r for r in rows if r not in stale_rows]
    open_rows = [r for r in rows if r.open_trade_qty > 0]
    desync_rows = [r for r in rows if r.desync]

    timeframe_counts = Counter(r.timeframe for r in rows)
    symbol_counts = Counter(r.symbol for r in rows)

    latest_updated = max((r.updated_at for r in rows if r.updated_at is not None), default=None)
    oldest_updated = min((r.updated_at for r in rows if r.updated_at is not None), default=None)

    strategy_names = sorted({r.strategy_name for r in rows if r.strategy_name})
    strategy_versions = sorted({r.strategy_version for r in rows if r.strategy_version})

    latest_supervisor = latest_file("paper_live_supervisor_*.json", results_dir)
    latest_audit = latest_file("paper_live_supervisor_audit_*.jsonl", results_dir)
    supervisor_payload = safe_read_json(latest_supervisor)
    supervisor_summary = supervisor_payload.get("summary") if isinstance(supervisor_payload, dict) and isinstance(supervisor_payload.get("summary"), dict) else {}
    state_trade_metrics = fetch_closed_trades_metrics([r.execution_id for r in rows])
    database_trade_metrics, db_latest_trade_time, db_latest_entry_time, db_latest_exit_time = fetch_database_trade_metrics(
        strategy_names, strategy_versions
    )
    scientific_trade_count, scientific_latest_entry_time, count_filter_reason = fetch_scientific_trade_metrics(
        strategy_names, strategy_versions
    )

    latest_state_update = max((r.updated_at for r in rows if r.updated_at is not None), default=None)
    state_age_seconds = None if latest_state_update is None else max(0.0, (datetime.now(timezone.utc) - latest_state_update).total_seconds())
    state_db_delta = int(state_trade_metrics["closed_trades"]) - int(database_trade_metrics["closed_trades"])
    count_delta = int(database_trade_metrics["closed_trades"]) - scientific_trade_count
    now_utc = datetime.now(timezone.utc)
    hours_since_last_entry = None if db_latest_entry_time is None else max(0.0, (now_utc - db_latest_entry_time).total_seconds() / 3600.0)
    hours_since_last_exit = None if db_latest_exit_time is None else max(0.0, (now_utc - db_latest_exit_time).total_seconds() / 3600.0)
    operational_state = "ACTIVE" if len(active_rows) == len(rows) else "DEGRADED" if active_rows else "OFFLINE"
    event_state = "QUIESCENT" if (
        hours_since_last_entry is None or hours_since_last_entry >= 24.0
    ) and (
        hours_since_last_exit is None or hours_since_last_exit >= 24.0
    ) else "ACTIVE"

    print("=" * 88)
    print("PAPER LIVE STATUS")
    print("=" * 88)
    print(f"campaign_id            : {campaign_id or '(vazio)'}")
    print(f"strategy_name(s)       : {', '.join(strategy_names) or 'n/a'}")
    print(f"strategy_version(s)    : {', '.join(strategy_versions) or 'n/a'}")
    print(f"operational_state      : {operational_state}")
    print(f"event_state            : {event_state}")
    print(f"contexts_total         : {len(rows)}")
    print(f"contexts_active        : {len(active_rows)}")
    print(f"contexts_stale         : {len(stale_rows)} (limite {args.max_stale_min:.1f} min)")
    print(f"open_trades            : {len(open_rows)}")
    print(f"desync_candidates      : {len(desync_rows)}")
    print(f"state_closed_trades    : {int(state_trade_metrics['closed_trades'])}")
    print(f"database_closed_trades : {int(database_trade_metrics['closed_trades'])}")
    print(f"scientific_trade_count : {scientific_trade_count}")
    print(f"count_delta            : {count_delta:+d}")
    print(f"count_filter_reason    : {count_filter_reason}")
    print(f"state_age              : {format_age(state_age_seconds)}")
    if db_latest_trade_time is not None:
        print(f"db_latest_trade_time   : {db_latest_trade_time.isoformat()}")
    else:
        print("db_latest_trade_time   : n/a")
    if db_latest_entry_time is not None:
        print(f"db_latest_entry_time   : {db_latest_entry_time.isoformat()}")
    else:
        print("db_latest_entry_time   : n/a")
    if db_latest_exit_time is not None:
        print(f"db_latest_exit_time    : {db_latest_exit_time.isoformat()}")
    else:
        print("db_latest_exit_time    : n/a")
    print(f"hours_since_last_entry : {hours_since_last_entry:.2f}h" if hours_since_last_entry is not None else "hours_since_last_entry : n/a")
    print(f"hours_since_last_exit  : {hours_since_last_exit:.2f}h" if hours_since_last_exit is not None else "hours_since_last_exit  : n/a")
    if state_db_delta != 0:
        print(f"snapshot_delta         : {state_db_delta:+d} (database is official source)")
    else:
        print("snapshot_delta         : 0 (database matches snapshot)")

    print("-" * 88)
    print("PERFORMANCE (DATABASE AS OFFICIAL SOURCE)")
    trade_metrics = database_trade_metrics
    print(f"net_profit             : {float(trade_metrics['net_profit']):+.6f}")
    print(f"win_rate               : {float(trade_metrics['win_rate']) * 100.0:.2f}%")
    print(f"profit_factor          : {float(trade_metrics['profit_factor']):.6f}")
    print(f"expectancy             : {float(trade_metrics['expectancy']):+.6f}")
    print(f"max_drawdown           : {float(trade_metrics['max_drawdown']):.6f}")
    print(f"sharpe                 : {float(trade_metrics['sharpe']):.6f}")
    print(f"wins                   : {int(trade_metrics['wins'])}")
    print(f"losses                 : {int(trade_metrics['losses'])}")

    if lags:
        print(f"lag_min                : {format_age(min(lags))}")
        print(f"lag_avg                : {format_age(mean(lags))}")
        print(f"lag_max                : {format_age(max(lags))}")
    else:
        print("lag_min                : n/a")
        print("lag_avg                : n/a")
        print("lag_max                : n/a")

    if latest_updated is not None:
        print(f"updated_at_latest_utc  : {latest_updated.isoformat()}")
    if oldest_updated is not None:
        print(f"updated_at_oldest_utc  : {oldest_updated.isoformat()}")

    print(f"timeframes             : {dict(sorted(timeframe_counts.items()))}")
    print(f"symbols                : {dict(sorted(symbol_counts.items()))}")

    if rows:
        cycles = [r.cycles for r in rows]
        print(f"cycles_min             : {min(cycles)}")
        print(f"cycles_avg             : {mean(cycles):.1f}")
        print(f"cycles_max             : {max(cycles)}")

    print("-" * 88)
    print(f"latest_supervisor_json : {latest_supervisor.name if latest_supervisor else 'n/a'}")
    print(f"latest_audit_jsonl     : {latest_audit.name if latest_audit else 'n/a'}")
    if supervisor_summary:
        print(f"supervisor_status      : {supervisor_summary.get('status', 'n/a')}")
        print(f"supervisor_contexts    : {supervisor_summary.get('contexts', 'n/a')}")
        print(f"supervisor_restarts    : {supervisor_summary.get('total_restarts', 'n/a')}")
        print(f"supervisor_failures    : {supervisor_summary.get('permanent_failures', 'n/a')}")

    if desync_rows:
        print("-" * 88)
        print("DESYNC CANDIDATES")
        for row in desync_rows:
            print(
                f"{row.symbol:12s} {row.timeframe:4s} required={row.open_trade_qty:.6f} "
                f"available={row.broker_available_qty:.6f} file={row.path.name}"
            )

    if args.show_contexts:
        print("-" * 88)
        print("CONTEXTS")
        for row in sorted(rows, key=lambda r: ((r.lag_seconds if r.lag_seconds is not None else 1e30), r.symbol, r.timeframe)):
            lag = format_age(row.lag_seconds)
            status = "STALE" if (row.lag_seconds is None or row.lag_seconds > stale_limit_seconds) else "OK"
            open_qty = f"{row.open_trade_qty:.6f}" if row.open_trade_qty > 0 else "0"
            print(
                f"{row.symbol:12s} {row.timeframe:4s} status={status:5s} lag={lag:>7s} "
                f"cycles={row.cycles:6d} open_qty={open_qty:>10s}"
            )

    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
