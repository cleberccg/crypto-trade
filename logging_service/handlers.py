from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from logging_service.formatter import build_formatter
from logging_service.logging_config import LoggingServiceConfig


def _truncate_file(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8"):
            pass
    except Exception:
        # Logging must never fail hard.
        return


def _gzip_rotator(source: str, dest: str) -> None:
    retries = 5
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with open(source, "rb") as src, gzip.open(dest + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                os.remove(source)
            except PermissionError:
                # Another process/thread still holds the file. Keep execution alive.
                _truncate_file(source)
            return
        except PermissionError as exc:
            last_error = exc
            # Backoff to allow transient Windows lock release.
            time.sleep(0.05 * (attempt + 1))
        except Exception as exc:
            last_error = exc
            break

    # Graceful degradation: keep service running, preserve forward logging.
    try:
        fallback_name = f"{dest}.fallback"
        shutil.copyfile(source, fallback_name)
        _truncate_file(source)
    except Exception:
        if last_error is not None:
            return


def _gzip_namer(default_name: str) -> str:
    return default_name


class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotation handler that degrades gracefully under transient file locks."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            # Keep current file and continue logging.
            return
        except Exception:
            return


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Timed rotation handler resilient to Windows file-lock contention."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            return
        except Exception:
            return


def _build_rotating_handler(file_path: Path, cfg: LoggingServiceConfig) -> RotatingFileHandler:
    handler = SafeRotatingFileHandler(
        file_path,
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(build_formatter())
    if cfg.compress_rotated:
        handler.rotator = _gzip_rotator
        handler.namer = _gzip_namer
    return handler


def _build_timed_handler(file_path: Path, cfg: LoggingServiceConfig) -> TimedRotatingFileHandler:
    handler = SafeTimedRotatingFileHandler(
        file_path,
        when=cfg.timed_when,
        interval=cfg.timed_interval,
        backupCount=cfg.timed_backup_count,
        encoding="utf-8",
        delay=True,
        utc=True,
    )
    handler.setFormatter(build_formatter())
    if cfg.compress_rotated:
        handler.rotator = _gzip_rotator
        handler.namer = _gzip_namer
    return handler


def build_disk_handlers(cfg: LoggingServiceConfig) -> list[logging.Handler]:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = []
    app_file = cfg.log_dir / "application.log"

    if cfg.enable_size_rotation:
        handlers.append(_build_rotating_handler(app_file, cfg))
    if cfg.enable_time_rotation:
        handlers.append(_build_timed_handler(cfg.log_dir / "application_timed.log", cfg))

    if cfg.enable_console:
        console = logging.StreamHandler()
        console.setFormatter(build_formatter())
        handlers.append(console)

    return handlers
