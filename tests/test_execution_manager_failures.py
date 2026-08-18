from __future__ import annotations

import os
from pathlib import Path

from execution_manager.manager import ExecutionManager


def _run_with_env(tmp_path: Path, key: str, value: str) -> int:
    original = os.getenv(key)
    os.environ[key] = value
    try:
        manager = ExecutionManager(tmp_path)
        return manager.run()
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def test_simulated_thread_stopped_creates_incident(tmp_path: Path) -> None:
    rc = _run_with_env(tmp_path, "EXECUTION_MANAGER_SIMULATE_THREAD_STOP", "1")
    assert rc == 1
    incidents = list((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incidents


def test_simulated_worker_dead_creates_incident(tmp_path: Path) -> None:
    rc = _run_with_env(tmp_path, "EXECUTION_MANAGER_SIMULATE_WORKER_DEAD", "1")
    assert rc == 1
    incidents = list((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incidents


def test_simulated_timeout_creates_incident(tmp_path: Path) -> None:
    rc = _run_with_env(tmp_path, "EXECUTION_MANAGER_SIMULATE_TIMEOUT", "1")
    assert rc == 1
    incidents = list((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incidents


def test_simulated_subprocess_failure_creates_incident(tmp_path: Path) -> None:
    rc = _run_with_env(tmp_path, "EXECUTION_MANAGER_SIMULATE_SUBPROCESS_FAIL", "1")
    assert rc == 1
    incidents = list((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incidents


def test_simulated_unexpected_error_creates_incident(tmp_path: Path) -> None:
    rc = _run_with_env(tmp_path, "EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR", "1")
    assert rc == 1
    incidents = list((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incidents
