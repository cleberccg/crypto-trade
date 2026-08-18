from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoggingServiceConfig:
    log_dir: Path
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 7
    timed_when: str = "midnight"
    timed_interval: int = 1
    timed_backup_count: int = 7
    compress_rotated: bool = True
    queue_maxsize: int = 20000
    enable_console: bool = True
    enable_size_rotation: bool = True
    enable_time_rotation: bool = False
