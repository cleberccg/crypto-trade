from __future__ import annotations

import logging
from multiprocessing import Process
from pathlib import Path

from logging_service.logger_manager import (
    bind_worker_logging_queue,
    get_logging_queue_handle,
    initialize_logging_service,
    shutdown_logging_service,
)
from utils.logger import get_logger


def _worker(worker_id: int, count: int, queue) -> None:
    bind_worker_logging_queue(queue=queue, level="INFO")
    logger = get_logger(f"stress.worker.{worker_id}")
    for i in range(count):
        logger.info("worker=%s message=%s", worker_id, i)


def _run_with_workers(workers: int, messages: int, queue) -> None:
    procs: list[Process] = []
    for wid in range(workers):
        p = Process(target=_worker, args=(wid, messages, queue))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0


def test_logging_multiprocess_matrix(tmp_path: Path) -> None:
    initialize_logging_service(log_dir=tmp_path / "logs", level="INFO", queue_maxsize=50000, enable_console=False)
    try:
        queue = get_logging_queue_handle()
        assert queue is not None
        for workers in (2, 4, 8, 16, 32):
            _run_with_workers(workers=workers, messages=200, queue=queue)
    finally:
        shutdown_logging_service()

    files = list((tmp_path / "logs").glob("*.log*"))
    assert files, "expected log files to be created"
