from __future__ import annotations

import logging
from logging.handlers import QueueListener
from typing import Any


class LoggingListener:
    def __init__(self, queue: Any, handlers: list[logging.Handler]) -> None:
        self._listener = QueueListener(queue, *handlers, respect_handler_level=True)
        self._handlers = handlers
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._listener.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self._listener.stop()
        for handler in self._handlers:
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        self._running = False

    @property
    def running(self) -> bool:
        return self._running
