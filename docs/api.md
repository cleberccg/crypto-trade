## Execution Manager API

### REST

- GET `/api/v1/execution`
- GET `/api/v1/execution/status`
- GET `/api/v1/execution/jobs`
- GET `/api/v1/execution/progress`
- GET `/api/v1/execution/performance`
- GET `/api/v1/execution/heartbeat`
- GET `/api/v1/execution/watchdog`
- GET `/api/v1/execution/report`
- GET `/api/v1/execution/incidents`
- GET `/api/v1/execution/logs`
- GET `/api/v1/execution-metrics`
- GET `/api/v1/notifications/telegram/status`
- POST `/api/v1/notifications/telegram/command`
- GET `/api/v1/execution/{execution_id}`
- GET `/api/v1/execution/{execution_id}/timeline`
- GET `/api/v1/execution/{execution_id}/jobs`
- GET `/api/v1/execution/{execution_id}/metrics`
- GET `/api/v1/execution/{execution_id}/artifacts`
- POST `/api/v1/execution/pause`
- POST `/api/v1/execution/resume`
- POST `/api/v1/execution/cancel`
- POST `/api/v1/execution/retry`

### WebSocket

- `/api/v1/ws/execution`
- `/api/v1/ws/progress`
- `/api/v1/ws/jobs`
- `/api/v1/ws/performance`
- `/api/v1/ws/heartbeat`
# API - Next Phase Preparation

All endpoints below are exposed under `/api/v1/` and are currently mock/read-only
to avoid interference with the active long-running optimization execution.

## Jobs
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/running`
- `GET /jobs/history`

## Timeline / Notifications / Scheduler
- `GET /timeline`
- `GET /notifications`
- `GET /scheduler`

Realtime-safe streams (authenticated token query param):
- `WS /ws/timeline`
- `WS /ws/notifications`
- `WS /ws/scheduler`

## Research
- `GET /research`
- `GET /research/comparisons`
- `GET /research/rankings`
- `GET /research/insights`
- `GET /research/heatmaps`
- `GET /research/reports`

## Scanner / System Status
- `GET /scanner`
- `GET /dashboard/status`

## Next Phase Activation (Dry Run)
- `GET /next-phase/readiness`
- `GET /next-phase/activation-plan`

## Activation Boundary
Until the optimizer execution finishes, all routes above remain read-only and
mock-backed. No runtime auto-execution is enabled.