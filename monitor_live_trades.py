#!/usr/bin/env python
"""Monitor live trade intent, execution and errors.

This script is meant to run continuously in a terminal while the live bot is
running. It watches the application log and the database, then prints concise
alerts when it detects:
- new BUY / SELL intent
- risk approval / rejection
- order execution
- open / closed trades
- errors or crashes in the live loop
- BUY intent without execution within a configurable grace period
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Iterable

from sqlalchemy import text

from database.connection import get_db
from utils.logger import get_logger

logger = get_logger(__name__)


LIVE_PATTERNS = {
    "candle": re.compile(r"Novo candle recebido", re.IGNORECASE),
    "indicators": re.compile(r"Indicadores recalculados", re.IGNORECASE),
    "signal_buy": re.compile(r"Sinal gerado .*signal=BUY", re.IGNORECASE),
    "signal_sell": re.compile(r"Sinal gerado .*signal=SELL", re.IGNORECASE),
    "risk_approved": re.compile(r"Entrada aprovada pelo RiskManager", re.IGNORECASE),
    "risk_rejected": re.compile(r"Entrada rejeitada|abortando nova entrada|saldo insuficiente", re.IGNORECASE),
    "order_buy": re.compile(r"Compra executada", re.IGNORECASE),
    "order_sell": re.compile(r"Stop ou take profit executados|Venda executada", re.IGNORECASE),
    # Avoid false positives like "nenhuma posicao aberta encontrada" during startup reconciliation.
    "position_open": re.compile(r"Posicao aberta\s*-\s*trade_id=", re.IGNORECASE),
    "position_closed": re.compile(r"Posicao fechada\s*-\s*trade_id=", re.IGNORECASE),
    "startup_clean": re.compile(r"Reconciliacao: nenhuma posicao aberta encontrada", re.IGNORECASE),
    "error": re.compile(r"ERROR|Traceback|NetworkError|OperationalError|InvalidOrder|BadRequest|BadSymbol|InsufficientFunds", re.IGNORECASE),
}


@dataclass
class MonitorState:
    last_log_offset: int = 0
    last_buy_intent_at: datetime | None = None
    last_buy_intent_line: str | None = None
    started_at_utc: datetime | None = None
    last_report_key: tuple[int, int, int] | None = None
    recent_alerts: deque[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.recent_alerts is None:
            self.recent_alerts = deque(maxlen=20)


def _timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _print(message: str) -> None:
    print(f"{_timestamp()} | {message}", flush=True)


def _tail_new_lines(path: Path, state: MonitorState) -> list[str]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(state.last_log_offset)
        lines = handle.readlines()
        state.last_log_offset = handle.tell()
    return lines


def _classify_line(line: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in LIVE_PATTERNS.items():
        if pattern.search(line):
            hits.append(name)
    return hits


def _extract_log_timestamp(line: str) -> datetime | None:
    try:
        prefix = line.split(" | ", 1)[0].strip()
        return datetime.strptime(prefix, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _poll_database() -> dict[str, object]:
    engine = get_db().engine
    with engine.connect() as conn:
        trade_counts = conn.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_trades,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
                    COUNT(*) AS total_trades
                FROM trades
                """
            )
        ).mappings().one()

        order_counts = conn.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status IN ('OPEN', 'NEW', 'PARTIALLY_FILLED') THEN 1 ELSE 0 END) AS open_orders,
                    COUNT(*) AS total_orders
                FROM orders
                """
            )
        ).mappings().one()

        last_trade = conn.execute(
            text(
                """
                SELECT id, symbol, status, side, entry_price, exit_price, entry_time, exit_time, exit_reason
                FROM trades
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).mappings().first()

        last_order = conn.execute(
            text(
                """
                SELECT id, trade_id, symbol, side, status, order_type, price, quantity, exchange_order_id, timestamp
                FROM orders
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).mappings().first()

    return {
        "open_trades": int(trade_counts["open_trades"] or 0),
        "closed_trades": int(trade_counts["closed_trades"] or 0),
        "total_trades": int(trade_counts["total_trades"] or 0),
        "open_orders": int(order_counts["open_orders"] or 0),
        "total_orders": int(order_counts["total_orders"] or 0),
        "last_trade": dict(last_trade) if last_trade else None,
        "last_order": dict(last_order) if last_order else None,
    }


def _maybe_alert(state: MonitorState, message: str) -> None:
    if message in state.recent_alerts:
        return
    state.recent_alerts.append(message)
    _print(f"ALERTA | {message}")


def _report_database_state(state: MonitorState, report: dict[str, object]) -> None:
    key = (
        int(report["total_trades"]),
        int(report["open_trades"]),
        int(report["total_orders"]),
    )
    if state.last_report_key == key:
        return
    state.last_report_key = key
    _print(
        "DB | trades total={total_trades} open={open_trades} closed={closed_trades} | "
        "orders total={total_orders} open_like={open_orders}".format(**report)
    )

    last_trade = report.get("last_trade")
    if last_trade:
        _print(
            "DB | last_trade id={id} symbol={symbol} status={status} side={side} "
            "entry={entry_price} exit={exit_price} reason={exit_reason}".format(**last_trade)
        )

    last_order = report.get("last_order")
    if last_order:
        _print(
            "DB | last_order id={id} trade_id={trade_id} symbol={symbol} side={side} "
            "status={status} type={order_type} qty={quantity} price={price}".format(**last_order)
        )


def run_monitor(log_path: Path, poll_seconds: float, watch_db: bool, buy_grace_seconds: int, follow_only: bool) -> None:
    state = MonitorState()
    state.started_at_utc = datetime.now(timezone.utc)
    _print(f"Monitor iniciado | log={log_path} | poll_seconds={poll_seconds} | watch_db={watch_db}")

    if not follow_only and watch_db:
        try:
            report = _poll_database()
            _report_database_state(state, report)
        except Exception as exc:
            _maybe_alert(state, f"Falha ao consultar banco na inicializacao: {exc}")

    while True:
        try:
            if log_path.exists():
                for line in _tail_new_lines(log_path, state):
                    stripped = line.rstrip("\n")
                    hits = _classify_line(stripped)
                    if not hits:
                        continue

                    timestamp = _extract_log_timestamp(stripped)
                    is_historical = bool(
                        timestamp is not None
                        and state.started_at_utc is not None
                        and timestamp < state.started_at_utc
                    )
                    if "signal_buy" in hits:
                        if is_historical:
                            _print(f"HISTORICO | BUY | {stripped}")
                        else:
                            state.last_buy_intent_at = timestamp or datetime.now(timezone.utc)
                            state.last_buy_intent_line = stripped
                            _print(f"INTENCAO | BUY | {stripped}")
                    elif "signal_sell" in hits:
                        if is_historical:
                            _print(f"HISTORICO | SELL | {stripped}")
                        else:
                            _print(f"INTENCAO | SELL | {stripped}")
                    elif "risk_approved" in hits:
                        if is_historical:
                            _print(f"HISTORICO | risco aprovado | {stripped}")
                        else:
                            _print(f"RISCO | aprovado | {stripped}")
                    elif "risk_rejected" in hits:
                        if is_historical:
                            _print(f"HISTORICO | risco rejeitado | {stripped}")
                        else:
                            _maybe_alert(state, f"Risco rejeitou entrada: {stripped}")
                    elif "order_buy" in hits:
                        if is_historical:
                            _print(f"HISTORICO | compra | {stripped}")
                        else:
                            _print(f"EXECUCAO | compra | {stripped}")
                        state.last_buy_intent_at = None
                        state.last_buy_intent_line = None
                    elif "order_sell" in hits:
                        if is_historical:
                            _print(f"HISTORICO | saida | {stripped}")
                        else:
                            _print(f"EXECUCAO | saida | {stripped}")
                    elif "position_open" in hits:
                        if is_historical:
                            _print(f"HISTORICO | posicao aberta | {stripped}")
                        else:
                            _print(f"POSICAO | aberta | {stripped}")
                        state.last_buy_intent_at = None
                        state.last_buy_intent_line = None
                    elif "position_closed" in hits:
                        if is_historical:
                            _print(f"HISTORICO | posicao fechada | {stripped}")
                        else:
                            _print(f"POSICAO | fechada | {stripped}")
                    elif "startup_clean" in hits:
                        _print(f"STARTUP | sem posicao aberta | {stripped}")
                    elif "error" in hits:
                        if is_historical:
                            _print(f"HISTORICO | erro | {stripped}")
                        else:
                            _maybe_alert(state, f"Erro detectado no live: {stripped}")

            if watch_db:
                report = _poll_database()
                _report_database_state(state, report)

                if report["open_trades"] > 0 and report["total_orders"] == 0:
                    _maybe_alert(state, "Existe trade aberto no banco, mas nenhuma ordem registrada ainda.")

            if state.last_buy_intent_at is not None:
                age = datetime.now(timezone.utc) - state.last_buy_intent_at
                if age.total_seconds() >= buy_grace_seconds:
                    _maybe_alert(
                        state,
                        "BUY sem execucao dentro da janela de tolerancia. "
                        f"Ultima intencao: {state.last_buy_intent_line or 'n/a'}",
                    )
                    state.last_buy_intent_at = None
                    state.last_buy_intent_line = None

        except KeyboardInterrupt:
            _print("Monitor encerrado pelo usuario.")
            return
        except Exception as exc:
            _maybe_alert(state, f"Falha no monitor: {exc}")

        sleep(max(1.0, float(poll_seconds)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor live trades and execution errors.")
    parser.add_argument(
        "--log-path",
        default=str(Path(__file__).parent / "logs" / "application.log"),
        help="Path to the main application log.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="Polling interval in seconds.",
    )
    parser.add_argument(
        "--buy-grace-seconds",
        type=int,
        default=90,
        help="Alert if a BUY intent is not followed by execution within this window.",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Disable database polling and only watch the log file.",
    )
    parser.add_argument(
        "--follow-only",
        action="store_true",
        help="Start tailing from the current end of the log instead of reading the backlog.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    log_path = Path(args.log_path)
    if not log_path.is_absolute():
        log_path = (Path(__file__).parent / log_path).resolve()

    run_monitor(
        log_path=log_path,
        poll_seconds=float(args.poll_seconds),
        watch_db=not bool(args.no_db),
        buy_grace_seconds=int(args.buy_grace_seconds),
        follow_only=bool(args.follow_only),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
