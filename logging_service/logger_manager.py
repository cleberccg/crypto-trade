from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from multiprocessing import Queue, current_process
from pathlib import Path
from time import perf_counter
from typing import Any

from logging_service.handlers import build_disk_handlers
from logging_service.listener import LoggingListener
from logging_service.logging_config import LoggingServiceConfig
from logging_service.queue_logger import configure_process_logger
from logging_service.shutdown import register_shutdown


class _LoggingRuntime:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.initialized = False
        self.cfg: LoggingServiceConfig | None = None
        self.queue: Queue | None = None
        self.listener: LoggingListener | None = None
        self.level = logging.INFO
        self.overflow_counter: dict[str, int] = {"dropped": 0, "errors": 0}
        self.metrics_counter: dict[str, int] = {"messages": 0}
        self.started_at: datetime | None = None
        self.last_rotation_at: str | None = None
        self.flush_ms: float | None = None
        self.rotate_ms: float | None = None


_RUNTIME = _LoggingRuntime()


def initialize_logging_service(
    log_dir: Path,
    level: str = "INFO",
    queue_maxsize: int = 20000,
    enable_console: bool = True,
    enable_time_rotation: bool = False,
) -> None:
    with _RUNTIME.lock:
        if _RUNTIME.initialized:
            same_config = (
                _RUNTIME.cfg is not None
                and _RUNTIME.cfg.log_dir == log_dir
                and _RUNTIME.cfg.level.upper() == level.upper()
                and _RUNTIME.cfg.queue_maxsize == queue_maxsize
                and _RUNTIME.cfg.enable_console == enable_console
                and _RUNTIME.cfg.enable_time_rotation == enable_time_rotation
            )
            if same_config:
                return

            # Reconfigure in place to support test isolation and runtime changes.
            if _RUNTIME.listener is not None:
                _RUNTIME.listener.stop()
            _RUNTIME.initialized = False

        cfg = LoggingServiceConfig(
            log_dir=log_dir,
            level=level,
            queue_maxsize=queue_maxsize,
            enable_console=enable_console,
            enable_time_rotation=enable_time_rotation,
        )

        queue: Queue = Queue(maxsize=cfg.queue_maxsize)
        handlers = build_disk_handlers(cfg)
        listener = LoggingListener(queue=queue, handlers=handlers)
        listener.start()

        _RUNTIME.cfg = cfg
        _RUNTIME.queue = queue
        _RUNTIME.listener = listener
        _RUNTIME.level = getattr(logging, level.upper(), logging.INFO)
        _RUNTIME.initialized = True
        _RUNTIME.started_at = datetime.now(tz=timezone.utc)

        register_shutdown(shutdown_logging_service)


def ensure_initialized_from_settings() -> None:
    if _RUNTIME.initialized:
        return
    # Child processes must be explicitly bound to a shared queue by caller code.
    if current_process().name != "MainProcess":
        return
    from config.settings import settings

    initialize_logging_service(
        log_dir=settings.logging.log_dir,
        level=settings.logging.level,
        queue_maxsize=int(os.getenv("LOG_QUEUE_MAXSIZE", "20000")),
        enable_console=True,
        enable_time_rotation=os.getenv("LOG_ENABLE_TIME_ROTATION", "0") == "1",
    )


def get_logger(name: str) -> logging.Logger:
    ensure_initialized_from_settings()
    if _RUNTIME.queue is None:
        # Fallback only when logger is requested before explicit startup.
        ensure_initialized_from_settings()
    if _RUNTIME.queue is None:
        # Worker process fallback: use basic logger without queue
        if current_process().name != "MainProcess":
            logger = logging.getLogger(name)
            if not logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s')
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                level = _RUNTIME.level if isinstance(_RUNTIME.level, int) else getattr(logging, _RUNTIME.level, logging.INFO)
                logger.setLevel(level)
            return logger
        raise RuntimeError("Logging service queue is not configured for this process.")

    logger = configure_process_logger(
        name=name,
        level=_RUNTIME.level,
        queue=_RUNTIME.queue,
        overflow_counter=_RUNTIME.overflow_counter,
        metrics_counter=_RUNTIME.metrics_counter,
    )
    return logger


def shutdown_logging_service() -> None:
    with _RUNTIME.lock:
        if not _RUNTIME.initialized:
            return

        start = perf_counter()
        listener = _RUNTIME.listener
        if listener is not None:
            listener.stop()
        flush_elapsed = (perf_counter() - start) * 1000

        _RUNTIME.flush_ms = flush_elapsed
        _RUNTIME.initialized = False


def get_logging_status() -> dict[str, Any]:
    with _RUNTIME.lock:
        return {
            "initialized": _RUNTIME.initialized,
            "started_at": _RUNTIME.started_at.isoformat() if _RUNTIME.started_at else None,
            "listener_running": _RUNTIME.listener.running if _RUNTIME.listener else False,
            "queue_size": _RUNTIME.queue.qsize() if _RUNTIME.queue else 0,
            "queue_maxsize": _RUNTIME.cfg.queue_maxsize if _RUNTIME.cfg else 0,
            "dropped_messages": _RUNTIME.overflow_counter.get("dropped", 0),
            "errors": _RUNTIME.overflow_counter.get("errors", 0),
            "log_dir": str(_RUNTIME.cfg.log_dir) if _RUNTIME.cfg else None,
            "config": asdict(_RUNTIME.cfg) if _RUNTIME.cfg else None,
        }


def get_logging_performance() -> dict[str, Any]:
    with _RUNTIME.lock:
        elapsed = None
        if _RUNTIME.started_at is not None:
            elapsed = max(0.001, (datetime.now(tz=timezone.utc) - _RUNTIME.started_at).total_seconds())
        msgs = _RUNTIME.metrics_counter.get("messages", 0)
        return {
            "messages": msgs,
            "messages_per_second": (msgs / elapsed) if elapsed else 0.0,
            "flush_ms": _RUNTIME.flush_ms,
            "rotation_ms": _RUNTIME.rotate_ms,
            "last_rotation_at": _RUNTIME.last_rotation_at,
        }


def get_logging_queue() -> dict[str, Any]:
    with _RUNTIME.lock:
        size = _RUNTIME.queue.qsize() if _RUNTIME.queue else 0
        maxsize = _RUNTIME.cfg.queue_maxsize if _RUNTIME.cfg else 0
        return {
            "size": size,
            "maxsize": maxsize,
            "utilization_pct": round((size / maxsize) * 100.0, 2) if maxsize else 0.0,
            "dropped_messages": _RUNTIME.overflow_counter.get("dropped", 0),
        }


def get_logging_listener() -> dict[str, Any]:
    with _RUNTIME.lock:
        return {
            "running": _RUNTIME.listener.running if _RUNTIME.listener else False,
            "started_at": _RUNTIME.started_at.isoformat() if _RUNTIME.started_at else None,
            "level": logging.getLevelName(_RUNTIME.level),
        }


def get_logging_files() -> dict[str, Any]:
    with _RUNTIME.lock:
        if not _RUNTIME.cfg:
            return {"items": []}
        items = []
        for path in sorted(_RUNTIME.cfg.log_dir.glob("*.log*")):
            try:
                stat = path.stat()
                items.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
            except OSError:
                continue
        return {"items": items}


def get_logging_queue_handle() -> Queue | None:
    with _RUNTIME.lock:
        return _RUNTIME.queue


def bind_worker_logging_queue(queue: Queue, level: str | int = "INFO") -> None:
    """Bind a spawned worker process to the shared logging queue (no local listener)."""
    with _RUNTIME.lock:
        _RUNTIME.queue = queue
        _RUNTIME.listener = None
        _RUNTIME.level = getattr(logging, str(level).upper(), logging.INFO) if isinstance(level, str) else int(level)
        _RUNTIME.initialized = True
