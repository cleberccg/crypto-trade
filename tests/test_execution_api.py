from __future__ import annotations

from fastapi.testclient import TestClient


def test_execution_endpoints_exist(monkeypatch) -> None:
    monkeypatch.setenv("API_USER", "admin")
    monkeypatch.setenv("API_PASSWORD", "admin")
    monkeypatch.setenv("API_ROLE", "administrator")
    monkeypatch.setenv("API_JWT_SECRET", "this-is-a-test-jwt-secret-32-plus")

    from webapi.app import create_app

    client = TestClient(create_app())
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for path in [
        "/api/v1/execution",
        "/api/v1/execution/status",
        "/api/v1/execution/jobs",
        "/api/v1/execution/progress",
        "/api/v1/execution/performance",
        "/api/v1/execution/heartbeat",
        "/api/v1/execution/watchdog",
        "/api/v1/execution/report",
        "/api/v1/execution/incidents",
        "/api/v1/execution-metrics",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200

    execution_id = client.get("/api/v1/execution/status", headers=headers).json().get("execution_id")
    if execution_id:
        for path in [
            f"/api/v1/execution/{execution_id}",
            f"/api/v1/execution/{execution_id}/timeline",
            f"/api/v1/execution/{execution_id}/jobs",
            f"/api/v1/execution/{execution_id}/metrics",
            f"/api/v1/execution/{execution_id}/artifacts",
        ]:
            response = client.get(path, headers=headers)
            assert response.status_code == 200

    for path in [
        "/api/v1/execution/pause",
        "/api/v1/execution/resume",
        "/api/v1/execution/cancel",
        "/api/v1/execution/retry",
    ]:
        response = client.post(path, headers=headers)
        assert response.status_code == 200
