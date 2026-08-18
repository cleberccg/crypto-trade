from __future__ import annotations

import logging


def build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(processName)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
