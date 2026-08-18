from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically using a sibling temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with tmp_path.open("w", encoding=encoding, newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, path)
