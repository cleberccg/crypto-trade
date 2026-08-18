from __future__ import annotations

from fastapi.testclient import TestClient

from webapi.app import app


def test_logging_endpoints_auth() -> None:
    client = TestClient(app)

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "viewer"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for endpoint in (
        "/api/v1/logging/status",
        "/api/v1/logging/performance",
        "/api/v1/logging/queue",
        "/api/v1/logging/listener",
        "/api/v1/logging/files",
    ):
        r = client.get(endpoint, headers=headers)
        assert r.status_code == 200
