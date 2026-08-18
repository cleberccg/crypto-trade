from __future__ import annotations

import os
from pathlib import Path

from execution_manager.manager import ExecutionManager


def test_incident_bundle_contains_required_files(tmp_path: Path) -> None:
    original = os.getenv("EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR")
    os.environ["EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR"] = "1"
    try:
        manager = ExecutionManager(tmp_path)
        manager.run()
    finally:
        if original is None:
            os.environ.pop("EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR", None)
        else:
            os.environ["EXECUTION_MANAGER_SIMULATE_UNEXPECTED_ERROR"] = original

    incident_dirs = sorted((tmp_path / "optimization" / "results" / "incidents").glob("INC_*"))
    assert incident_dirs
    latest = incident_dirs[-1]

    required = [
        latest / "incident.json",
        latest / "stacktrace.txt",
        latest / "environment.txt",
        latest / "execution_state.json",
        latest / "checkpoint_reference.txt",
        latest / "logs.zip",
    ]
    for path in required:
        assert path.exists(), f"missing {path}"
