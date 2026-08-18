from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient


def test_night_runner_health_endpoint_reads_heartbeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("API_USER", "admin")
    monkeypatch.setenv("API_PASSWORD", "admin")
    monkeypatch.setenv("API_ROLE", "administrator")
    monkeypatch.setenv("API_JWT_SECRET", "this-is-a-test-jwt-secret-32-plus")

    from webapi.app import create_app

    heartbeat_path = Path("optimization/results/night_runner_heartbeat.json")
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "running",
        "execution_id": "exec-health-001",
        "pid": 1234,
        "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "last_checkpoint_at": datetime.now(timezone.utc).isoformat(),
        "last_log_at": datetime.now(timezone.utc).isoformat(),
        "last_db_update_at": datetime.now(timezone.utc).isoformat(),
        "last_combo": "BTC/USDT 5m",
        "last_processed": 2500,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stalled_seconds": 3.0,
    }
    heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")

    client = TestClient(create_app())
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    response = client.get(
        "/api/v1/health/night-runner",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"Running", "Idle", "Blocked", "Recovering", "Completed"}
    assert data["execution_id"] == "exec-health-001"
