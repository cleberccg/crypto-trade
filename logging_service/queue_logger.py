from __future__ import annotations

import logging
from logging.handlers import QueueHandler
from queue import Full
from typing import Any


class SafeQueueHandler(QueueHandler):
    """Queue handler that never raises to callers when logging backend is saturated."""

    def __init__(self, queue: Any, overflow_counter: dict[str, int], metrics_counter: dict[str, int]) -> None:
        super().__init__(queue)
        self._overflow_counter = overflow_counter
        self._metrics_counter = metrics_counter

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
            self._metrics_counter["messages"] = self._metrics_counter.get("messages", 0) + 1
        except Full:
            self._overflow_counter["dropped"] = self._overflow_counter.get("dropped", 0) + 1
        except Exception:
            self._overflow_counter["errors"] = self._overflow_counter.get("errors", 0) + 1


def configure_process_logger(
    name: str,
    level: int,
    queue: Any,
    overflow_counter: dict[str, int],
    metrics_counter: dict[str, int],
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        logger.handlers.clear()

    logger.addHandler(SafeQueueHandler(queue, overflow_counter, metrics_counter))
    return logger
