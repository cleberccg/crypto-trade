from __future__ import annotations

import json

from paper_trading.campaign_registry_store import upsert_campaign_registry_execution


def test_upsert_registry_merges_and_deduplicates_execution_ids(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"

    first = upsert_campaign_registry_execution(
        registry_path=registry_path,
        campaign_id="spc-1",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        execution_ids=["exec-1", "exec-2"],
    )
    second = upsert_campaign_registry_execution(
        registry_path=registry_path,
        campaign_id="spc-1",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        execution_ids=["exec-2", "exec-3", ""],
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = payload["campaigns"]["spc-1"]

    assert first["execution_ids"] == ["exec-1", "exec-2"]
    assert second["execution_ids"] == ["exec-1", "exec-2", "exec-3"]
    assert entry["execution_ids"] == ["exec-1", "exec-2", "exec-3"]
    assert entry["created_at"]
    assert entry["updated_at"]
