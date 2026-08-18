from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def event_message(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    title = f"[{event_type}]"
    execution_id = payload.get("execution_id") or payload.get("execution") or "-"
    symbol = payload.get("symbol") or payload.get("asset") or "-"
    timeframe = payload.get("timeframe") or "-"
    details = payload.get("details") or payload.get("message") or ""
    message = (
        f"Evento: {event_type}\n"
        f"Execution ID: {execution_id}\n"
        f"Ativo: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Detalhes: {details}"
    )
    return title, message


def periodic_progress(snapshot: dict[str, Any]) -> tuple[str, str]:
    title = "[progress_report]"
    message = (
        f"Execution ID: {snapshot.get('execution_id')}\n"
        f"Status: {snapshot.get('status')}\n"
        f"Pipeline: {snapshot.get('pipeline', 'night_runner')}\n"
        f"Ativo: {snapshot.get('current_asset')}\n"
        f"Timeframe: {snapshot.get('current_timeframe')}\n"
        f"Progresso: {snapshot.get('progress_pct')}%\n"
        f"Combinacoes: {snapshot.get('processed_total')}/{snapshot.get('target_total')}\n"
        f"Workers: {snapshot.get('workers')}\n"
        f"ETA(s): {snapshot.get('eta_seconds')}\n"
        f"CPU: {snapshot.get('cpu')}\n"
        f"RAM: {snapshot.get('ram')}\n"
        f"Melhor PF: {snapshot.get('best_profit_factor')}\n"
        f"Melhor Sharpe: {snapshot.get('best_sharpe')}\n"
        f"Melhor Drawdown: {snapshot.get('best_drawdown')}\n"
        f"Trades: {snapshot.get('trade_count')}\n"
        f"Candles: {snapshot.get('candle_count')}\n"
        f"Banco atualizado: {snapshot.get('database_updated')}\n"
        f"Ultimo checkpoint: {snapshot.get('last_checkpoint')}\n"
        f"Ultimo heartbeat: {snapshot.get('last_heartbeat')}"
    )
    return title, message


def command_help() -> str:
    return (
        "Comandos disponiveis:\n"
        "/start\n/help\n/status\n/health\n/execution\n/progress\n/ranking\n"
        "/metrics\n/incidents\n/logs\n/checkpoints\n/artifacts\n/version"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
