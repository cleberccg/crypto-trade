"""Backward-compatible logger facade delegating to logging_service."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from logging_service.logger_manager import get_logger as _get_logger
from logging_service.logger_manager import initialize_logging_service


def setup_logger(
    name: str,
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    log_to_file: bool = True,
) -> logging.Logger:
    if log_to_file and log_dir is not None:
        initialize_logging_service(log_dir=log_dir, level=level)
    return _get_logger(name)


def get_logger(name: str) -> logging.Logger:
    return _get_logger(name)
