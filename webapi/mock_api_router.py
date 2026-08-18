from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from webapi.dependencies import require_user
from webapi.security import JWT_ALGORITHM, JWT_SECRET
from webapi.mock_services import (
    execution_timeline_snapshot,
    job_detail,
    jobs_history,
    jobs_running,
    jobs_snapshot,
    mock_dashboard_status,
    notifications_snapshot,
    research_comparisons_snapshot,
    research_heatmaps_snapshot,
    research_insights_snapshot,
    research_rankings_snapshot,
    research_reports_snapshot,
    research_snapshot,
    scanner_snapshot,
    scheduler_snapshot,
)
from activation.readiness_service import activation_plan_snapshot, readiness_snapshot

router = APIRouter(tags=["mock-platform"])


def _execution_stub() -> dict[str, Any]:
    """Backward-compatible payload for legacy execution API routes."""
    return {
        "execution_id": None,
        "status": "idle",
        "message": "mock execution endpoint",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/jobs")
def get_jobs(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return jobs_snapshot()


@router.get("/jobs/running")
def get_running_jobs(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return jobs_running()


@router.get("/jobs/history")
def get_jobs_history(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return jobs_history()


@router.get("/jobs/{job_id}")
def get_job(job_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    payload = job_detail(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return payload


@router.get("/timeline")
def get_timeline(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return execution_timeline_snapshot()


@router.get("/notifications")
def get_notifications(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return notifications_snapshot()


@router.get("/scheduler")
def get_scheduler(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return scheduler_snapshot()


@router.get("/research")
def get_research(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_snapshot()


@router.get("/research/comparisons")
def get_research_comparisons(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_comparisons_snapshot()


@router.get("/research/rankings")
def get_research_rankings(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_rankings_snapshot()


@router.get("/research/insights")
def get_research_insights(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_insights_snapshot()


@router.get("/research/heatmaps")
def get_research_heatmaps(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_heatmaps_snapshot()


@router.get("/research/reports")
def get_research_reports(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return research_reports_snapshot()


@router.get("/scanner")
def get_scanner(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return scanner_snapshot()


@router.get("/dashboard/status")
def get_dashboard_status(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    payload = mock_dashboard_status()
    return {
        "meta": {"page": 1, "page_size": 1, "total": 1},
        "items": [payload],
    }


@router.get("/execution")
def execution_root(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return _execution_stub()


@router.get("/execution/status")
def execution_status(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return _execution_stub()


@router.get("/execution/jobs")
def execution_jobs(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return jobs_snapshot()


@router.get("/execution/progress")
def execution_progress(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "progress_pct": 0.0,
    }


@router.get("/execution/performance")
def execution_performance(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "cpu": 0.0,
        "memory": 0.0,
    }


@router.get("/execution/heartbeat")
def execution_heartbeat(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "alive": True,
    }


@router.get("/execution/watchdog")
def execution_watchdog(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "healthy": True,
    }


@router.get("/execution/report")
def execution_report(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "items": [],
    }


@router.get("/execution/incidents")
def execution_incidents(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "items": [],
    }


@router.get("/execution-metrics")
def execution_metrics(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "items": [],
    }


@router.get("/execution/{execution_id}")
def execution_by_id(execution_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "execution_id": execution_id,
    }


@router.get("/execution/{execution_id}/timeline")
def execution_timeline(execution_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "execution_id": execution_id,
        "items": [],
    }


@router.get("/execution/{execution_id}/jobs")
def execution_jobs_by_id(execution_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "execution_id": execution_id,
        "items": [],
    }


@router.get("/execution/{execution_id}/metrics")
def execution_metrics_by_id(execution_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "execution_id": execution_id,
        "items": [],
    }


@router.get("/execution/{execution_id}/artifacts")
def execution_artifacts(execution_id: str, _user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {
        **_execution_stub(),
        "execution_id": execution_id,
        "items": [],
    }


@router.post("/execution/pause")
def execution_pause(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {**_execution_stub(), "action": "pause", "accepted": True}


@router.post("/execution/resume")
def execution_resume(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {**_execution_stub(), "action": "resume", "accepted": True}


@router.post("/execution/cancel")
def execution_cancel(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {**_execution_stub(), "action": "cancel", "accepted": True}


@router.post("/execution/retry")
def execution_retry(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return {**_execution_stub(), "action": "retry", "accepted": True}


@router.get("/next-phase/readiness")
def get_next_phase_readiness(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return readiness_snapshot()


@router.get("/next-phase/activation-plan")
def get_next_phase_activation_plan(_user: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
    return activation_plan_snapshot()


def _authorize_ws_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return False
    return bool(payload.get("sub"))


@router.websocket("/ws/timeline")
async def ws_timeline(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not _authorize_ws_token(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            await websocket.send_json(
                {
                    "event": "timeline_tick",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "snapshot": execution_timeline_snapshot(),
                }
            )
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return


@router.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not _authorize_ws_token(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            await websocket.send_json(
                {
                    "event": "notifications_tick",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "snapshot": notifications_snapshot(),
                }
            )
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return


@router.websocket("/ws/scheduler")
async def ws_scheduler(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not _authorize_ws_token(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            await websocket.send_json(
                {
                    "event": "scheduler_tick",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "snapshot": scheduler_snapshot(),
                }
            )
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return
