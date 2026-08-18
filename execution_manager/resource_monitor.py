from __future__ import annotations

import threading
import time

import psutil


class ResourceMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cpu = 0.0
        self.ram = 0.0
        self.disk = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(2):
            self.cpu = psutil.cpu_percent(interval=None)
            self.ram = psutil.virtual_memory().percent
            self.disk = psutil.disk_usage("/").percent

    def snapshot(self) -> dict[str, float]:
        return {"cpu": self.cpu, "ram": self.ram, "disk": self.disk}
