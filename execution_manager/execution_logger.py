from __future__ import annotations

from pathlib import Path

from utils.logger import setup_logger


class ExecutionLoggers:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.execution = setup_logger("execution_manager", log_dir=log_dir, log_to_file=True)
        self.execution_main = setup_logger("execution", log_dir=log_dir, log_to_file=True)
        self.optimizer_real = setup_logger("optimizer_real", log_dir=log_dir, log_to_file=True)
        self.heartbeat = setup_logger("heartbeat", log_dir=log_dir, log_to_file=True)
        self.watchdog = setup_logger("watchdog", log_dir=log_dir, log_to_file=True)
        self.progress = setup_logger("progress", log_dir=log_dir, log_to_file=True)
        self.performance = setup_logger("performance", log_dir=log_dir, log_to_file=True)
        self.jobs = setup_logger("jobs", log_dir=log_dir, log_to_file=True)
        self.scheduler = setup_logger("scheduler", log_dir=log_dir, log_to_file=True)
        self.recovery = setup_logger("recovery", log_dir=log_dir, log_to_file=True)
        self.errors = setup_logger("errors", log_dir=log_dir, log_to_file=True)
