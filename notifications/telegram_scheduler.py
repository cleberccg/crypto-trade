from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from notifications.telegram_templates import periodic_progress


class TelegramScheduler:
    def __init__(self, interval_minutes: int, sender: Callable[[str, str, str], None]) -> None:
        self._interval_seconds = max(1, int(interval_minutes)) * 60
        self._sender = sender
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            snapshot = self._build_snapshot()
            title, message = periodic_progress(snapshot)
            self._sender("progress_report", title, message)

    def _build_snapshot(self) -> dict:
        heartbeat_file = Path(__file__).resolve().parents[1] / "optimization" / "results" / "night_runner_heartbeat.json"
        if heartbeat_file.exists():
            import json

            try:
                hb = json.loads(heartbeat_file.read_text(encoding="utf-8"))
            except Exception:
                hb = {}
        else:
            hb = {}
        return {
            "execution_id": hb.get("execution_id"),
            "status": hb.get("state", "unknown"),
            "pipeline": "night_runner",
            "current_asset": hb.get("current_asset") or hb.get("symbol"),
            "current_timeframe": hb.get("current_timeframe") or hb.get("timeframe"),
            "progress_pct": hb.get("progress_pct", 0.0),
            "processed_total": hb.get("last_processed", 0),
            "target_total": hb.get("target_total", 0),
            "eta_seconds": hb.get("eta_seconds"),
            "workers": hb.get("workers"),
            "cpu": hb.get("cpu"),
            "ram": hb.get("ram"),
            "best_profit_factor": hb.get("best_profit_factor"),
            "best_sharpe": hb.get("best_sharpe"),
            "best_drawdown": hb.get("best_drawdown"),
            "trade_count": hb.get("trade_count"),
            "candle_count": hb.get("candle_count"),
            "database_updated": hb.get("last_db_update_at"),
            "last_checkpoint": hb.get("last_checkpoint_at"),
            "last_heartbeat": hb.get("last_heartbeat_at"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
