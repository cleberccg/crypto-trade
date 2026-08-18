from __future__ import annotations

import atexit
from typing import Callable


_registered = False


def register_shutdown(callback: Callable[[], None]) -> None:
    global _registered
    if _registered:
        return
    atexit.register(callback)
    _registered = True
