from __future__ import annotations

from execution_manager.resource_monitor import ResourceMonitor


class PerformanceService:
    def __init__(self, monitor: ResourceMonitor) -> None:
        self.monitor = monitor

    def snapshot(self) -> dict[str, float]:
        return self.monitor.snapshot()
