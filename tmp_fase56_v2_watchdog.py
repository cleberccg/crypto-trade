from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("optimization/results")
STATUS_PATH = BASE / "fase56_v2_equivalence_status.json"
BENCH_JSON = BASE / "fase56_v2_equivalence_benchmark.json"
WATCHDOG_STATUS = BASE / "fase56_v2_watchdog_status.json"
WATCHDOG_LOG = BASE / "fase56_v2_watchdog.log"

CHECK_INTERVAL_SECONDS = 60
STALE_SECONDS = 12 * 60
MAX_RESTARTS = 6


class WatchdogError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_log(message: str) -> None:
    line = f"[{utc_now()}] {message}\n"
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def write_watchdog_status(state: str, extra: dict | None = None) -> None:
    payload: dict = {
        "generated_at": utc_now(),
        "state": state,
    }
    if extra:
        payload.update(extra)
    WATCHDOG_STATUS.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def start_benchmark() -> subprocess.Popen:
    append_log("Starting benchmark process")
    return subprocess.Popen(
        [sys.executable, "tmp_fase56_v2_equivalence_benchmark.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0,
    )


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    append_log(f"Terminating process pid={proc.pid}")
    try:
        if sys.platform.startswith("win"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            time.sleep(2)
            if proc.poll() is None:
                proc.kill()
        else:
            proc.terminate()
            time.sleep(2)
            if proc.poll() is None:
                proc.kill()
    except Exception as exc:
        append_log(f"Error terminating process pid={proc.pid}: {exc}")


def status_freshness_seconds() -> float | None:
    if not STATUS_PATH.exists():
        return None
    return max(0.0, time.time() - STATUS_PATH.stat().st_mtime)


def status_snapshot() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return read_json(STATUS_PATH)
    except Exception:
        return {}


def is_completed() -> bool:
    if not STATUS_PATH.exists() or not BENCH_JSON.exists():
        return False
    try:
        status = read_json(STATUS_PATH)
    except Exception:
        return False
    return status.get("state") == "completed"


def run_watchdog() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    write_watchdog_status("starting", {"restarts": 0})

    restarts = 0
    proc = start_benchmark()

    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)

        if is_completed():
            append_log("Benchmark completed successfully")
            write_watchdog_status(
                "completed",
                {
                    "restarts": restarts,
                    "status_file": str(STATUS_PATH),
                    "benchmark_file": str(BENCH_JSON),
                },
            )
            terminate_process(proc)
            return 0

        freshness = status_freshness_seconds()
        status = status_snapshot()
        proc_exit = proc.poll()

        write_watchdog_status(
            "running",
            {
                "restarts": restarts,
                "process_pid": proc.pid,
                "process_exit_code": proc_exit,
                "status_state": status.get("state"),
                "status_stage": status.get("stage"),
                "status_progress_pct": status.get("progress_pct"),
                "status_generated_at": status.get("generated_at"),
                "status_stale_seconds": round(freshness, 2) if freshness is not None else None,
                "check_interval_seconds": CHECK_INTERVAL_SECONDS,
                "stale_seconds_threshold": STALE_SECONDS,
            },
        )

        if proc_exit is not None:
            append_log(f"Process exited with code={proc_exit}")
            if restarts >= MAX_RESTARTS:
                raise WatchdogError("Max restarts reached after process exits")
            restarts += 1
            proc = start_benchmark()
            continue

        if freshness is None:
            append_log("Status file does not exist yet; waiting")
            continue

        if freshness > STALE_SECONDS:
            append_log(f"Detected stale status ({freshness:.1f}s), restarting process")
            terminate_process(proc)
            if restarts >= MAX_RESTARTS:
                raise WatchdogError("Max restarts reached after stale detections")
            restarts += 1
            proc = start_benchmark()


def main() -> None:
    try:
        exit_code = run_watchdog()
    except Exception as exc:
        append_log(f"Watchdog failed: {exc}")
        write_watchdog_status("failed", {"error": str(exc)})
        raise
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
