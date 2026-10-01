"""Interfaces de observadores para listeners de eventos do otimizador."""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.events.events import OptimizationEvent


class EventListener(ABC):
    """Contrato de listener para receber eventos do ciclo de vida do otimizador."""

    @abstractmethod
    def handle(self, event: OptimizationEvent) -> None:
        """Processa um evento publicado."""
