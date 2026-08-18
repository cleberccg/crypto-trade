"""In-process event bus with optional asynchronous listener dispatch."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from threading import RLock
from typing import Iterable

from core.events.events import OptimizationEvent
from core.events.interfaces import EventListener
from utils.logger import get_logger

logger = get_logger(__name__)


class EventBus:
    """Simple observer dispatcher for optimizer events."""

    def __init__(self, listeners: Iterable[EventListener] | None = None, async_dispatch: bool = False) -> None:
        self._listeners: list[EventListener] = list(listeners or [])
        self._async_dispatch = async_dispatch
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=4) if async_dispatch else None

    def subscribe(self, listener: EventListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def publish(self, event: OptimizationEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)

        if not listeners:
            return

        if self._async_dispatch and self._executor is not None:
            futures = [self._executor.submit(_safe_handle, listener, event) for listener in listeners]
            wait(futures)
            return

        for listener in listeners:
            _safe_handle(listener, event)

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)


def _safe_handle(listener: EventListener, event: OptimizationEvent) -> None:
    try:
        listener.handle(event)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("Event listener failure: listener=%s event=%s error=%s", listener.__class__.__name__, event.event_type, exc)
