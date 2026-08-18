from __future__ import annotations

from datetime import datetime, timezone

from main import _parse_args
from paper_trading.specialized_campaign import (
    SpecializedPaperCampaignService,
    _advance_trade_markers,
    _campaign_answer,
    _merge_execution_ids,
    _normalize_execution_ids,
    _phase_complete,
    _trade_is_after_marker,
)


def test_phase_complete_requires_both_time_and_trades() -> None:
    assert _phase_complete(7, 50, 7, 50) is True
    assert _phase_complete(7, 49, 7, 50) is False
    assert _phase_complete(6, 50, 7, 50) is False


def test_campaign_answer_reflects_p1_p2_and_kill_switch() -> None:
    assert _campaign_answer(True, True, False) == "SIM"
    assert _campaign_answer(True, False, False) == "PARCIALMENTE"
    assert _campaign_answer(False, False, False) == "NAO"
    assert _campaign_answer(True, True, True) == "NAO"


def test_cli_parser_specialized_campaign(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "paper-specialized-campaign",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--campaign-id",
            "spc-test-001",
            "--contexts",
            "BTC/USDT:1h,ETH/USDT:1h",
            "--phase1-min-days",
            "7",
            "--phase2-min-trades",
            "200",
            "--legacy-live-execution",
            "--ingest-execution-ids",
            "exec-1,exec-2",
        ],
    )
    args = _parse_args()
    assert args.command == "paper-specialized-campaign"
    assert args.strategy_name == "ClassicDonchianBreakout"
    assert args.campaign_id == "spc-test-001"
    assert args.contexts == "BTC/USDT:1h,ETH/USDT:1h"
    assert args.phase1_min_days == 7
    assert args.phase2_min_trades == 200
    assert args.legacy_live_execution is True
    assert args.ingest_execution_ids == "exec-1,exec-2"


def test_cli_parser_paper_live_campaign_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "paper-live",
            "--symbol",
            "BTC/USDT",
            "--timeframe",
            "5m",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--campaign-id",
            "spc-test-001",
        ],
    )
    args = _parse_args()
    assert args.command == "paper-live"
    assert args.campaign_id == "spc-test-001"


def test_normalize_execution_ids_removes_invalid_and_duplicates() -> None:
    value = ["exec-1", "", None, "exec-2", "exec-1", "   exec-3   "]
    assert _normalize_execution_ids(value) == ["exec-1", "exec-2", "exec-3"]


def test_merge_execution_ids_appends_new_runs_only() -> None:
    existing = ["exec-a", "exec-b"]
    runs = [
        {"execution_id": "exec-b"},
        {"execution_id": "exec-c"},
        {"execution_id": None},
        {},
    ]
    assert _merge_execution_ids(existing, runs) == ["exec-a", "exec-b", "exec-c"]


def test_trade_marker_handles_same_timestamp_with_trade_id() -> None:
    marker_time = datetime(2026, 7, 2, 7, 30, tzinfo=timezone.utc)
    assert _trade_is_after_marker(marker_time, 102, marker_time, 101) is True
    assert _trade_is_after_marker(marker_time, 101, marker_time, 101) is False
    assert _trade_is_after_marker(marker_time, 100, marker_time, 101) is False


def test_advance_trade_markers_uses_last_id_when_timestamp_equal() -> None:
    marker_time = datetime(2026, 7, 2, 7, 30, tzinfo=timezone.utc)
    rows = [
        {"id": 101, "exit_time": "2026-07-02T07:30:00+00:00"},
        {"id": 102, "exit_time": "2026-07-02T07:30:00+00:00"},
        {"id": 103, "exit_time": "2026-07-02T07:30:00+00:00"},
    ]
    new_time, new_id = _advance_trade_markers(marker_time, 101, rows)
    assert new_time == marker_time
    assert new_id == 103


def test_decision_reason_blocks_promotion_for_outside_scope(tmp_path) -> None:
    service = SpecializedPaperCampaignService(base_dir=tmp_path)
    reason = service._decision_reason(
        answer="NAO",
        killed=False,
        p1_ok=False,
        p2_ok=False,
        metrics_ok=True,
        scope_ok=False,
        alert_state={"persistent_divergence": False},
    )
    assert reason == "Promotion blocked: Outside scope trades detected."