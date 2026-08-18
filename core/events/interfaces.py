"""Observer interfaces for optimizer event listeners."""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.events.events import OptimizationEvent


class EventListener(ABC):
    """Listener contract for receiving optimizer lifecycle events."""

    @abstractmethod
    def handle(self, event: OptimizationEvent) -> None:
        """Handle a published event."""
