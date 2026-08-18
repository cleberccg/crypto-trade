"""Concurrent-safe campaign registry persistence helpers."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _normalize_ids(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        output.append(token)
    return output


def upsert_campaign_registry_execution(
    *,
    registry_path: Path,
    campaign_id: str,
    strategy_name: str,
    strategy_version: str,
    execution_ids: list[str],
) -> dict[str, Any]:
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")

    with _exclusive_file_lock(lock_path):
        payload = _load_payload(registry_path)
        campaigns = payload.get("campaigns") if isinstance(payload.get("campaigns"), dict) else {}
        existing = campaigns.get(campaign_id) if isinstance(campaigns.get(campaign_id), dict) else {}
        merged_ids = _normalize_ids(
            [
                *(
                    existing.get("execution_ids")
                    if isinstance(existing.get("execution_ids"), list)
                    else []
                ),
                *execution_ids,
            ]
        )

        campaigns[campaign_id] = {
            "campaign_id": campaign_id,
            "strategy_name": strategy_name,
            "strategy_version": strategy_version,
            "created_at": existing.get("created_at") or now_iso,
            "updated_at": now_iso,
            "execution_ids": merged_ids,
        }

        out = {"generated_at": now_iso, "campaigns": campaigns}
        tmp_path = registry_path.with_suffix(registry_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp_path, registry_path)
        return campaigns[campaign_id]
