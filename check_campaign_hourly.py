from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "optimization" / "results"
STATE_FILE = RESULTS / "phase13_factory_state.json"
HEARTBEAT_FILES = [
    RESULTS / "execution_heartbeat.json",
    RESULTS / "night_runner_heartbeat.json",
]
LOG_FILE = RESULTS / "operational_mode_hourly_watchdog.log"
ALERT_FILE = RESULTS / "operational_mode_hourly_alerts.log"

CHECK_EVERY_SECONDS = 3600
STALL_AFTER_SECONDS = 3600


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    return path.stat().st_mtime


def _latest_heartbeat_mtime() -> float:
    return max((_mtime(p) for p in HEARTBEAT_FILES), default=0.0)


def _state_pending_count() -> int | None:
    if not STATE_FILE.exists():
        return None
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None

    if isinstance(payload, list):
        return sum(1 for item in payload if isinstance(item, dict) and item.get("state") in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"})
    if isinstance(payload, dict):
        backlog = payload.get("backlog")
        if isinstance(backlog, list):
            return sum(1 for item in backlog if isinstance(item, dict) and item.get("state") in {"IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"})
    return None


def _python_processes() -> list[str]:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | Select-Object -ExpandProperty CommandLine"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return [line for line in lines if "main.py overnight-campaign" in line]


def main() -> None:
    _append(LOG_FILE, f"[{_now()}] hourly watchdog started")
    last_progress = max(_mtime(STATE_FILE), _latest_heartbeat_mtime())
    last_pending = _state_pending_count()

    while True:
        now_ts = time.time()
        state_m = _mtime(STATE_FILE)
        hb_m = _latest_heartbeat_mtime()
        progress_m = max(state_m, hb_m)
        pending = _state_pending_count()
        proc = _python_processes()

        if progress_m > last_progress:
            last_progress = progress_m

        stagnant_for = int(max(0, now_ts - last_progress))
        status = "OK"
        reasons: list[str] = []

        if not proc:
            status = "ALERT"
            reasons.append("campaign_process_not_found")

        if stagnant_for >= STALL_AFTER_SECONDS:
            status = "ALERT"
            reasons.append(f"no_progress_for_{stagnant_for}s")

        if pending is not None and last_pending is not None and pending > last_pending:
            status = "ALERT"
            reasons.append(f"pending_increased_{last_pending}_to_{pending}")

        line = (
            f"[{_now()}] status={status} pending={pending} "
            f"state_mtime={datetime.fromtimestamp(state_m).isoformat() if state_m else 'missing'} "
            f"heartbeat_mtime={datetime.fromtimestamp(hb_m).isoformat() if hb_m else 'missing'} "
            f"stagnant_for={stagnant_for}s process_count={len(proc)}"
        )
        _append(LOG_FILE, line)

        if status == "ALERT":
            _append(ALERT_FILE, line + (" reasons=" + ",".join(reasons) if reasons else ""))

        last_pending = pending if pending is not None else last_pending
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
