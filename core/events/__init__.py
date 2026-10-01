"""Infraestrutura de eventos para orquestração desacoplada do otimizador."""

from core.events.event_bus import EventBus
from core.events.events import EventType, OptimizationEvent
from core.events.interfaces import EventListener

__all__ = [
    "EventBus",
    "EventType",
    "OptimizationEvent",
    "EventListener",
]
