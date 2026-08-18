from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from database import connection as connection_module
from database.connection import DatabaseConnection
from database.history_models import BacktestRun, TradeHistory
from database.session_models import ExecutionSession


def _seed_minimum_data(db: DatabaseConnection) -> str:
    execution_id = "exec-webapi-seed-001"
    now = datetime.now(timezone.utc)

    with db.session() as session:
        session.add(
            ExecutionSession(
                execution_id=execution_id,
                started_at=now - timedelta(hours=2),
                status="completed",
                host="test-host",
                cpu="test-cpu",
                workers=4,
                python_version="3.13",
                git_version="test",
                finished_at=now - timedelta(hours=1),
                duration=3600.0,
            )
        )

        session.add(
            BacktestRun(
                execution_id=execution_id,
                strategy="TrendV1",
                symbol="BTC/USDT",
                timeframe="5m",
                start_date=now - timedelta(days=3),
                end_date=now - timedelta(days=1),
                initial_capital=10000.0,
                final_capital=10500.0,
                total_trades=2,
                win_rate=50.0,
                profit_factor=1.2,
                sharpe=1.1,
                expectancy=0.2,
                drawdown=5.0,
                status="completed",
            )
        )

        session.add_all(
            [
                TradeHistory(
                    execution_id=execution_id,
                    strategy="TrendV1",
                    symbol="BTC/USDT",
                    timeframe="5m",
                    side="BUY",
                    entry_time=now - timedelta(days=2, hours=1),
                    exit_time=now - timedelta(days=2),
                    entry_price=100.0,
                    exit_price=102.0,
                    stop_loss=98.0,
                    take_profit=104.0,
                    risk_reward=2.0,
                    quantity=1.0,
                    pnl=2.0,
                    pnl_percent=2.0,
                    duration_minutes=60.0,
                    exit_reason="take_profit",
                    score=0.8,
                ),
                TradeHistory(
                    execution_id=execution_id,
                    strategy="TrendV1",
                    symbol="BTC/USDT",
                    timeframe="5m",
                    side="BUY",
                    entry_time=now - timedelta(days=1, hours=2),
                    exit_time=now - timedelta(days=1),
                    entry_price=102.0,
                    exit_price=101.0,
                    stop_loss=100.0,
                    take_profit=106.0,
                    risk_reward=2.0,
                    quantity=1.0,
                    pnl=-1.0,
                    pnl_percent=-0.98,
                    duration_minutes=120.0,
                    exit_reason="stop_loss",
                    score=0.5,
                ),
            ]
        )

    return execution_id


def test_webapi_roles_and_backtest_detail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("API_USER", "admin")
    monkeypatch.setenv("API_PASSWORD", "admin")
    monkeypatch.setenv("API_ROLE", "administrator")
    monkeypatch.setenv("API_OPERATOR_USER", "operator")
    monkeypatch.setenv("API_OPERATOR_PASSWORD", "operator")
    monkeypatch.setenv("API_VIEWER_USER", "viewer")
    monkeypatch.setenv("API_VIEWER_PASSWORD", "viewer")
    monkeypatch.setenv("API_JWT_SECRET", "this-is-a-test-jwt-secret-32-plus")
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("DEFAULT_SYMBOL", "BTC/USDT")
    monkeypatch.setenv("DEFAULT_TIMEFRAME", "5m")
    monkeypatch.setenv("OPTIMIZER_WORKERS", "12")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_test.db'}")

    db = DatabaseConnection(os.environ["DATABASE_URL"])
    previous_db = connection_module._db
    connection_module._db = db
    try:
        db.create_tables()

        execution_id = _seed_minimum_data(db)

        from webapi.app import create_app

        client = TestClient(create_app())

        login_viewer = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "viewer"})
        assert login_viewer.status_code == 200
        viewer_token = login_viewer.json()["access_token"]
        viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

        login_operator = client.post("/api/v1/auth/login", json={"username": "operator", "password": "operator"})
        assert login_operator.status_code == 200
        operator_token = login_operator.json()["access_token"]
        operator_headers = {"Authorization": f"Bearer {operator_token}"}

        dashboard_response = client.get("/api/v1/dashboard", headers=viewer_headers)
        assert dashboard_response.status_code == 200

        observability_response = client.get("/api/v1/observability", headers=viewer_headers)
        assert observability_response.status_code == 200
        observability_payload = observability_response.json()
        assert "running_executions" in observability_payload
        assert "recent_sessions" in observability_payload
        assert "host" in observability_payload

        with client.websocket_connect(f"/api/v1/ws/observability?token={viewer_token}") as websocket:
            tick = websocket.receive_json()
            assert tick["event"] == "observability_tick"
            assert "snapshot" in tick
            assert "running_executions" in tick["snapshot"]

        detail_response = client.get(f"/api/v1/backtests/{execution_id}", headers=viewer_headers)
        assert detail_response.status_code == 200
        payload = detail_response.json()
        assert payload["backtest"]["execution_id"] == execution_id
        assert len(payload["equity_curve"]) >= 1
        assert len(payload["trades"]) == 2

        viewer_update = client.put("/api/v1/settings", json={"workers": 20}, headers=viewer_headers)
        assert viewer_update.status_code == 403

        operator_update = client.put(
            "/api/v1/settings",
            json={"mode": "paper", "symbol": "BTC/USDT", "timeframe": "5m", "workers": 18},
            headers=operator_headers,
        )
        assert operator_update.status_code == 200
        assert operator_update.json()["workers"] == 18
    finally:
        connection_module._db = previous_db
        db.dispose()
