# Logging Architecture Audit

## Handlers and Creators

- RotatingFileHandler: created in logging_service/handlers.py via _build_rotating_handler (wrapped by SafeRotatingFileHandler)
- TimedRotatingFileHandler: created in logging_service/handlers.py via _build_timed_handler (wrapped by SafeTimedRotatingFileHandler)
- QueueListener: created in logging_service/listener.py (main process only)
- QueueHandler (SafeQueueHandler): created in logging_service/queue_logger.py and attached by configure_process_logger

## Process Ownership

- Main process owns disk handlers and QueueListener.
- Worker processes bind shared queue via bind_worker_logging_queue and do not open file handlers.

## Logger Entry Points

- utils/logger.py -> setup_logger/get_logger delegates to logging_service.logger_manager
- execution_manager/execution_logger.py uses setup_logger (queue-based)
- night_runner.py uses setup_logger (queue-based)

## File Handler Exposure Outside Main

- No direct FileHandler/RotatingFileHandler creation outside logging_service/handlers.py.
- Worker fallback in logger_manager uses StreamHandler only (no file).

## QueueHandler Coverage

- Core modules use get_logger, receiving queue-backed logger.
- Multiprocess workers are explicitly bound to queue in optimizer initializer.

## Risks Found

- Windows file-lock contention during gzip rotation of active file.
- Status inconsistencies in long-run records can mask operational state.

## Mitigations Implemented

- Safe rotating handlers with graceful rollover failure handling.
- Retry+backoff and fallback copy/truncate in gzip rotator.
- Watchdog no-progress detection with STALLED/RECOVERING transitions.

