# Logging Failure Analysis

## Incident Summary

- Error: PermissionError [WinError 32]
- File: logs/application.log
- Phase: long optimization campaign with multiprocessing workers
- Impact: logging rollover contention caused log subsystem churn and execution stall symptoms

## Root Cause

Rotation path attempted to compress/remove active log file while it was still locked by Windows.

Although architecture uses QueueHandler/QueueListener, rollover can still see transient lock contention.

## Corrective Actions

1. Safe rotating handlers (`SafeRotatingFileHandler`, `SafeTimedRotatingFileHandler`) now catch rollover errors.
2. Gzip rotator retries with backoff when file is locked.
3. On persistent lock, fallback copy + truncate strategy avoids hard failure.
4. Logging errors no longer bubble to optimizer/execution flow.

## Validation Criteria

- No process crash on rollover lock.
- No optimizer interruption due to logging failures.
- Continuous heartbeat and progress despite logging contention.
