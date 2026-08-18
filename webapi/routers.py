from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from config.settings import settings
from webapi.dependencies import get_db_session, require_operator_or_admin, require_user
from webapi.schemas import LoginRequest, LoginResponse, PaginatedMeta, PaginatedResponse
from webapi.schemas import NightRunnerHealthResponse
from webapi.security import authenticate, create_access_token
from webapi.services import (
    current_config_payload,
    get_execution_details,
    get_observability_snapshot,
    get_monitor_snapshot,
    get_analytics_snapshot,
    get_backtest_details,
    get_dashboard_snapshot,
    list_backtests,
    list_checkpoints,
    list_db_tables_snapshot,
    list_execution_sessions,
    list_indicators,
    list_optimizations,
    list_signals,
    list_strategy_versions,
    list_trades,
    list_validation_runs,
    optimization_top_results,
    read_logs,
    read_logs_as_text,
    stream_tail_lines,
    update_runtime_config,
)

router = APIRouter()
NIGHT_HEARTBEAT_FILE = Path(__file__).resolve().parents[1] / "optimization" / "results" / "night_runner_heartbeat.json"


def _empty_paginated(page: int, page_size: int) -> PaginatedResponse:
    return PaginatedResponse(
        meta=PaginatedMeta(page=page, page_size=page_size, total=0),
        items=[],
    )


@router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
def login(payload: LoginRequest) -> LoginResponse:
    user = authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user["username"], user["role"])
    return LoginResponse(access_token=token, role=user["role"])


@router.get("/dashboard", tags=["dashboard"])
def dashboard(
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return get_dashboard_snapshot(session)


@router.get("/executions", response_model=PaginatedResponse, tags=["executions"])
def executions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    status: str | None = Query(default=None),
    execution_type: str | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_execution_sessions(session, page, page_size, status, execution_type)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/execution-sessions", response_model=PaginatedResponse, tags=["executions"])
def execution_sessions_alias(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    status: str | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_execution_sessions(session, page, page_size, status, execution_type="session")
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/executions/{execution_id}", tags=["executions"])
def execution_detail(
    execution_id: str,
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    details = get_execution_details(session, execution_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return details


@router.get("/optimizations", response_model=PaginatedResponse, tags=["optimizer"])
def optimizations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    symbol: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    status: str | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_optimizations(session, page, page_size, symbol, timeframe, status)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/optimizations/{execution_id}/ranking", tags=["optimizer"])
def optimization_ranking(
    execution_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return {"items": optimization_top_results(session, execution_id, limit=limit)}


@router.get("/backtests", response_model=PaginatedResponse, tags=["backtests"])
def backtests(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    strategy: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_backtests(session, page, page_size, strategy, symbol)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/backtests/{execution_id}", tags=["backtests"])
def backtest_detail(
    execution_id: str,
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    details = get_backtest_details(session, execution_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return details


@router.get("/trades", response_model=PaginatedResponse, tags=["trades"])
def trades(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    symbol: str | None = Query(default=None),
    strategy: str | None = Query(default=None),
    min_pnl: float | None = Query(default=None),
    max_pnl: float | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_trades(session, page, page_size, symbol, strategy, min_pnl, max_pnl)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/signals", response_model=PaginatedResponse, tags=["signals"])
def signals(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    symbol: str | None = Query(default=None),
    strategy: str | None = Query(default=None),
    accepted: bool | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_signals(session, page, page_size, symbol, strategy, accepted, signal_type)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/indicators", response_model=PaginatedResponse, tags=["indicators"])
def indicators(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    min_rsi: float | None = Query(default=None),
    max_rsi: float | None = Query(default=None),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_indicators(session, page, page_size, min_rsi=min_rsi, max_rsi=max_rsi)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/analytics", tags=["analytics"])
def analytics(
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return get_analytics_snapshot(session)


@router.get("/validation", response_model=PaginatedResponse, tags=["validation"])
def validation_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_validation_runs(session, page, page_size)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/execution-checkpoints", response_model=PaginatedResponse, tags=["executions"])
def checkpoints(
    execution_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_checkpoints(session, execution_id, page, page_size)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/strategies", response_model=PaginatedResponse, tags=["strategies"])
def strategy_versions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_strategy_versions(session, page, page_size)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/database", response_model=PaginatedResponse, tags=["database"])
def database_readonly(
    table_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse:
    items, total = list_db_tables_snapshot(session, page, page_size, table_name)
    return PaginatedResponse(meta=PaginatedMeta(page=page, page_size=page_size, total=total), items=items)


@router.get("/logs", tags=["logs"])
def logs(
    level: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    _user: dict[str, str] = Depends(require_user),
) -> dict[str, Any]:
    return {"items": read_logs(level=level, q=q, max_lines=limit)}


@router.get("/logs/download", response_class=PlainTextResponse, tags=["logs"])
def logs_download(
    level: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    _user: dict[str, str] = Depends(require_user),
) -> str:
    return read_logs_as_text(level=level, q=q, max_lines=limit)


@router.get("/settings", tags=["settings"])
def get_settings_endpoint(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return current_config_payload()


@router.put("/settings", tags=["settings"])
def update_settings_endpoint(
    payload: dict[str, str | int],
    _user: dict[str, str] = Depends(require_operator_or_admin),
) -> dict[str, Any]:
    return update_runtime_config(payload)


@router.get("/health/night-runner", response_model=NightRunnerHealthResponse, tags=["system"])
def night_runner_health(_user: dict[str, str] = Depends(require_user)) -> NightRunnerHealthResponse:
    now = datetime.now(timezone.utc)
    if NIGHT_HEARTBEAT_FILE.exists():
        payload = json.loads(NIGHT_HEARTBEAT_FILE.read_text(encoding="utf-8"))
        return NightRunnerHealthResponse(
            status=str(payload.get("state", "unknown")),
            timestamp=now,
            execution_id=payload.get("execution_id"),
            pid=payload.get("pid"),
            state=str(payload.get("state", "unknown")),
            heartbeat=payload.get("last_heartbeat_at"),
            last_checkpoint=payload.get("last_checkpoint_at"),
            last_log=payload.get("last_log_at"),
            last_database_update=payload.get("last_db_update_at"),
            last_combination=payload.get("last_combo"),
            eta_minutes=None,
        )
    return NightRunnerHealthResponse(
        status="missing",
        timestamp=now,
        execution_id=None,
        pid=None,
        state="missing",
        heartbeat=None,
        last_checkpoint=None,
        last_log=None,
        last_database_update=None,
        last_combination=None,
        eta_minutes=None,
    )


@router.get("/monitor", tags=["monitor"])
def monitor_snapshot(
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return get_monitor_snapshot(session)


@router.get("/jobs", response_model=PaginatedResponse, tags=["jobs"])
def jobs_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
) -> PaginatedResponse:
    return _empty_paginated(page, page_size)


@router.get("/notifications", response_model=PaginatedResponse, tags=["notifications"])
def notifications_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
) -> PaginatedResponse:
    return _empty_paginated(page, page_size)


@router.get("/research", response_model=PaginatedResponse, tags=["research"])
def research_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
) -> PaginatedResponse:
    return _empty_paginated(page, page_size)


@router.get("/scanner", response_model=PaginatedResponse, tags=["scanner"])
def scanner_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
    _user: dict[str, str] = Depends(require_user),
) -> PaginatedResponse:
    return _empty_paginated(page, page_size)


@router.get("/observability", tags=["observability"])
def observability_snapshot(
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    monitor = get_monitor_snapshot(session)
    return {
        "status": "ok",
        "monitor": monitor,
    }


@router.get("/observability", tags=["monitor"])
def observability_snapshot(
    _user: dict[str, str] = Depends(require_user),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return get_observability_snapshot(session)


@router.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket) -> None:
    await websocket.accept()
    log_path = Path(settings.logging.log_dir) / "main.log"
    try:
        while True:
            lines = stream_tail_lines(log_path, limit=20)
            await websocket.send_json(
                {
                    "event": "monitor_tick",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tail": lines,
                }
            )
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return


@router.websocket("/ws/observability")
async def ws_observability(websocket: WebSocket) -> None:
    # Compatibility websocket channel expected by some frontend screens.
    await ws_monitor(websocket)


@router.websocket("/ws/observability")
async def ws_observability(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return

    try:
        require_user(token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    session = next(get_db_session())
    try:
        while True:
            payload = get_observability_snapshot(session)
            await websocket.send_json(
                {
                    "event": "observability_tick",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "snapshot": payload,
                }
            )
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return
    finally:
        session.close()
