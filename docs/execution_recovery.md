# Execution Recovery

## State Machine

- CREATED
- STARTING
- RUNNING
- CHECKPOINTING
- FINALIZING
- COMPLETED
- FAILED
- RECOVERING
- STALLED
- ABANDONED

## Stall Detection

Watchdog evaluates:

- heartbeat freshness
- checkpoint freshness
- db/log activity freshness
- explicit no-progress window (`last_progress_at`)

When critical threshold is reached, execution enters STALLED.

## Automatic Recovery

1. Mark execution RECOVERING.
2. Persist checkpoint/state.
3. Requeue RUNNING/FAILED jobs as WAITING with RECOVERED transition.
4. Return to RUNNING.

Recovery preserves execution_id and persisted progress.

## Safety Guarantees

- No reset to zero when progress exists.
- Incidents are recorded for every stall event.
- Notification events are emitted for started/progress/stalled/recovered/completed/failed.
