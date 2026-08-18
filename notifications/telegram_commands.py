from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func

from config.settings import settings
from database.connection import get_session
from database.history_models import ExecutionCheckpoint, OptimizationResultRecord, OptimizationRun
from notifications.telegram_templates import command_help


@dataclass(frozen=True)
class CommandResponse:
    ok: bool
    text: str


class TelegramCommands:
    def __init__(self, authorized_chat_ids: set[str] | None = None) -> None:
        self._authorized_chat_ids = authorized_chat_ids or set()

    def handle(self, command: str, chat_id: str) -> CommandResponse:
        cmd = (command or "").strip().lower()
        if not cmd.startswith("/"):
            return CommandResponse(ok=False, text="Comando invalido.")

        if cmd in {"/start", "/help"}:
            return CommandResponse(ok=True, text=command_help())

        if chat_id not in self._authorized_chat_ids:
            return CommandResponse(ok=False, text="Nao autorizado para comandos administrativos.")

        if cmd == "/status":
            return CommandResponse(ok=True, text=self._status())
        if cmd == "/health":
            return CommandResponse(ok=True, text=self._health())
        if cmd == "/execution":
            return CommandResponse(ok=True, text=self._execution())
        if cmd == "/progress":
            return CommandResponse(ok=True, text=self._progress())
        if cmd == "/ranking":
            return CommandResponse(ok=True, text=self._ranking())
        if cmd == "/metrics":
            return CommandResponse(ok=True, text=self._metrics())
        if cmd == "/incidents":
            return CommandResponse(ok=True, text=self._incidents())
        if cmd == "/logs":
            return CommandResponse(ok=True, text=self._logs())
        if cmd == "/checkpoints":
            return CommandResponse(ok=True, text=self._checkpoints())
        if cmd == "/artifacts":
            return CommandResponse(ok=True, text=self._artifacts())
        if cmd == "/version":
            return CommandResponse(ok=True, text="Crypto Bot Telegram Monitor v1")

        return CommandResponse(ok=False, text="Comando nao suportado.")

    def _status(self) -> str:
        with get_session() as session:
            run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
            if not run:
                return "Sem execucao ativa."
            return f"status={run.status} execution_id={run.execution_id} {run.symbol} {run.timeframe}"

    def _health(self) -> str:
        hb = Path(__file__).resolve().parents[1] / "optimization" / "results" / "night_runner_heartbeat.json"
        return "health=online" if hb.exists() else "health=missing_heartbeat"

    def _execution(self) -> str:
        with get_session() as session:
            run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
            if not run:
                return "execution=none"
            return f"execution_id={run.execution_id} symbol={run.symbol} timeframe={run.timeframe} status={run.status}"

    def _progress(self) -> str:
        with get_session() as session:
            run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
            if not run:
                return "progress=0"
            processed = session.query(func.count(OptimizationResultRecord.id)).filter(
                OptimizationResultRecord.execution_id == run.execution_id
            ).scalar() or 0
            total = int(run.total_combinations or 0)
            pct = (processed / total * 100.0) if total > 0 else 0.0
            return f"progress={processed}/{total} ({pct:.2f}%)"

    def _ranking(self) -> str:
        with get_session() as session:
            run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
            if not run:
                return "ranking=none"
            best = (
                session.query(OptimizationResultRecord)
                .filter(OptimizationResultRecord.execution_id == run.execution_id)
                .order_by(desc(OptimizationResultRecord.profit_factor), desc(OptimizationResultRecord.net_profit))
                .first()
            )
            if not best:
                return "ranking=sem_resultados"
            return f"best_pf={best.profit_factor} sharpe={best.sharpe} drawdown={best.drawdown}"

    def _metrics(self) -> str:
        return f"workers={settings.optimizer.workers} mode={settings.trading.mode} db={settings.database.type}"

    def _incidents(self) -> str:
        inc_dir = Path(__file__).resolve().parents[1] / "optimization" / "results" / "incidents"
        if not inc_dir.exists():
            return "incidents=0"
        count = len(list(inc_dir.glob("INC_*")))
        return f"incidents={count}"

    def _logs(self) -> str:
        log_dir = Path(settings.logging.log_dir)
        files = sorted(log_dir.glob("*.log"))
        if not files:
            return "logs=none"
        latest = files[-1]
        return f"latest_log={latest.name} size={latest.stat().st_size}"

    def _checkpoints(self) -> str:
        with get_session() as session:
            cp = session.query(ExecutionCheckpoint).order_by(desc(ExecutionCheckpoint.id)).first()
            if not cp:
                return "checkpoints=none"
            return f"checkpoint execution_id={cp.execution_id} stage={cp.stage} processed={cp.processed} completed={cp.completed}"

    def _artifacts(self) -> str:
        root = Path(__file__).resolve().parents[1] / "optimization" / "results"
        names = [
            "research_campaign_report.html",
            "research_campaign_report.json",
            "research_campaign_report.txt",
            "strategy_ranking.csv",
            "executive_strategy_report.md",
        ]
        available = [name for name in names if (root / name).exists()]
        return "artifacts=" + (", ".join(available) if available else "none")
