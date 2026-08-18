from __future__ import annotations

from fastapi.testclient import TestClient

from webapi.app import create_app


def test_new_platform_modules_smoke() -> None:
    app = create_app()
    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/jobs" in route_paths
    assert "/api/v1/research" in route_paths

    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "viewer"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    endpoints = [
        "/api/v1/jobs",
        "/api/v1/jobs/running",
        "/api/v1/jobs/history",
        "/api/v1/timeline",
        "/api/v1/notifications",
        "/api/v1/scheduler",
        "/api/v1/research",
        "/api/v1/research/comparisons",
        "/api/v1/research/rankings",
        "/api/v1/research/insights",
        "/api/v1/research/heatmaps",
        "/api/v1/research/reports",
        "/api/v1/scanner",
        "/api/v1/dashboard/status",
        "/api/v1/next-phase/readiness",
        "/api/v1/next-phase/activation-plan",
    ]

    for path in endpoints:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert "items" in payload or "system_health" in payload

    with client.websocket_connect(f"/api/v1/ws/timeline?token={token}") as ws_timeline:
        tick = ws_timeline.receive_json()
        assert tick["event"] == "timeline_tick"
        assert "snapshot" in tick

    with client.websocket_connect(f"/api/v1/ws/notifications?token={token}") as ws_notifications:
        tick = ws_notifications.receive_json()
        assert tick["event"] == "notifications_tick"
        assert "snapshot" in tick

    with client.websocket_connect(f"/api/v1/ws/scheduler?token={token}") as ws_scheduler:
        tick = ws_scheduler.receive_json()
        assert tick["event"] == "scheduler_tick"
        assert "snapshot" in tick
