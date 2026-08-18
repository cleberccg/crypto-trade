from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from strategies.classic_catalog_strategies import ClassicDonchianBreakoutStrategy

import paper_trading.paper_live_service as live_module
from paper_trading.paper_broker import PaperBroker
from paper_trading.paper_trader import OpenPosition, PaperTrader
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService


def test_should_generate_reports_first_run_is_true() -> None:
    now = datetime.now(tz=timezone.utc)
    assert PaperLiveService._should_generate_reports(
        now=now,
        last_report_at=None,
        current_closed_trades=0,
        last_report_closed_trades=0,
        min_interval_seconds=900,
        on_trade_change=True,
    )


def test_should_generate_reports_respects_throttle_without_progress() -> None:
    now = datetime.now(tz=timezone.utc)
    assert PaperLiveService._should_generate_reports(
        now=now,
        last_report_at=now - timedelta(seconds=120),
        current_closed_trades=10,
        last_report_closed_trades=10,
        min_interval_seconds=900,
        on_trade_change=True,
    ) is False


def test_should_generate_reports_on_trade_change_before_interval() -> None:
    now = datetime.now(tz=timezone.utc)
    assert PaperLiveService._should_generate_reports(
        now=now,
        last_report_at=now - timedelta(seconds=120),
        current_closed_trades=11,
        last_report_closed_trades=10,
        min_interval_seconds=900,
        on_trade_change=True,
    ) is True


def test_resume_campaign_id_mismatch_raises(tmp_path) -> None:
    service = PaperLiveService(base_dir=tmp_path)
    state_key = service._state_key("ClassicDonchianBreakout", "BTC/USDT", "5m")
    service._save_state(
        state_key,
        {
            "execution_id": "exec-1",
            "campaign_id": "campaign-state",
        },
    )

    cfg = PaperLiveConfig(
        symbol="BTC/USDT",
        timeframe="5m",
        strategy_name="ClassicDonchianBreakout",
        campaign_id="campaign-cli",
        max_cycles=1,
    )

    with pytest.raises(RuntimeError, match="Campaign ID mismatch"):
        service.run(cfg)


def test_link_execution_to_campaign_calls_atomic_upsert(monkeypatch, tmp_path) -> None:
    service = PaperLiveService(base_dir=tmp_path)
    captured: dict[str, object] = {}

    def _fake_upsert(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(live_module, "upsert_campaign_registry_execution", _fake_upsert)

    service._link_execution_to_campaign(
        campaign_id="spc-1",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        execution_id="exec-abc",
    )

    assert captured["campaign_id"] == "spc-1"
    assert captured["strategy_name"] == "ClassicDonchianBreakout"
    assert captured["strategy_version"] == "v1.0"
    assert captured["execution_ids"] == ["exec-abc"]


def test_runtime_state_roundtrip_keeps_max_holding_minutes() -> None:
    broker = PaperBroker(initial_capital=10_000.0)
    trader = PaperTrader(
        strategy=ClassicDonchianBreakoutStrategy(),
        broker=broker,
        execution_id="exec-1",
        timeframe="5m",
        strategy_version="v1.0",
    )
    trader._open_trade = OpenPosition(
        symbol="BTC/USDT",
        entry_price=100.0,
        quantity=1.0,
        stake_amount=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        trailing_stop=0.015,
        max_holding_minutes=180,
        risk_reward=2.0,
        score=1.0,
        entry_time=datetime.now(tz=timezone.utc),
    )
    broker.import_runtime_state({"cash": 9900.0, "positions": {"BTC": 1.0}})

    state = trader.export_runtime_state()
    restored = PaperTrader(
        strategy=ClassicDonchianBreakoutStrategy(),
        broker=PaperBroker(initial_capital=10_000.0),
        execution_id="exec-2",
        timeframe="5m",
        strategy_version="v1.0",
    )
    restored.import_runtime_state(state)

    assert restored._open_trade is not None
    assert restored._open_trade.max_holding_minutes == 180


def test_runtime_state_roundtrip_restores_broker_position() -> None:
    broker = PaperBroker(initial_capital=10_000.0)
    trader = PaperTrader(
        strategy=ClassicDonchianBreakoutStrategy(),
        broker=broker,
        execution_id="exec-1",
        timeframe="5m",
        strategy_version="v1.0",
    )
    entry_time = datetime.now(tz=timezone.utc)
    trader._open_trade = OpenPosition(
        symbol="ETH/USDT",
        entry_price=100.0,
        quantity=1.5,
        stake_amount=150.0,
        stop_loss=95.0,
        take_profit=110.0,
        trailing_stop=0.015,
        max_holding_minutes=180,
        risk_reward=2.0,
        score=1.0,
        entry_time=entry_time,
    )
    broker.import_runtime_state({"cash": 9850.0, "positions": {"ETH": 1.5}})

    state = trader.export_runtime_state()

    restored = PaperTrader(
        strategy=ClassicDonchianBreakoutStrategy(),
        broker=PaperBroker(initial_capital=10_000.0),
        execution_id="exec-2",
        timeframe="5m",
        strategy_version="v1.0",
    )
    restored.import_runtime_state(state)

    assert restored._open_trade is not None
    assert restored._open_trade.quantity == pytest.approx(1.5)
    assert restored._broker.get_position_quantity("ETH/USDT") == pytest.approx(1.5)


def test_import_runtime_state_drops_open_trade_when_broker_has_no_position() -> None:
    restored = PaperTrader(
        strategy=ClassicDonchianBreakoutStrategy(),
        broker=PaperBroker(initial_capital=10_000.0),
        execution_id="exec-3",
        timeframe="5m",
        strategy_version="v1.0",
    )
    state = {
        "open_trade": {
            "symbol": "ETH/USDT",
            "entry_price": 100.0,
            "quantity": 1.0,
            "stake_amount": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "trailing_stop": 0.015,
            "max_holding_minutes": 180,
            "risk_reward": 2.0,
            "score": 1.0,
            "entry_time": datetime.now(tz=timezone.utc).isoformat(),
        },
        "highest_price_since_entry": 101.0,
        "broker": {"cash": 10_000.0, "positions": {}},
    }

    restored.import_runtime_state(state)

    assert restored._open_trade is None
    assert restored._stats["cancelled_orders"] == 1


def test_load_state_falls_back_to_backup_when_primary_is_corrupted(tmp_path) -> None:
    service = PaperLiveService(base_dir=tmp_path)
    state_key = service._state_key("ClassicDonchianBreakout", "BTC/USDT", "5m")
    state_file = service._state_file_for(state_key)

    service._save_state(state_key, {"execution_id": "exec-ok", "cycles": 7})
    state_file.write_text("{broken-json", encoding="utf-8")

    restored = service._load_state(state_key)
    assert restored.get("execution_id") == "exec-ok"
    assert int(restored.get("cycles") or 0) == 7


def test_effective_hypothesis_payload_restores_from_state_when_config_missing() -> None:
    restored = PaperLiveService._effective_hypothesis_payload(
        config_payload=None,
        state_payload={
            "approved_parameters": {"entry_step": 4},
            "approved_filters": ["gate_flag >= 1"],
            "regime": "bullish|high_volatility",
        },
    )

    assert restored["approved_parameters"]["entry_step"] == 4
    assert restored["approved_filters"] == ["gate_flag >= 1"]
    assert restored["regime"] == "bullish|high_volatility"


def test_paper_live_bound_frame_caps_memory_after_thousands_of_cycles() -> None:
    start = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    frame = live_module.pd.DataFrame(
        [{"open": 99.9, "high": 100.1, "low": 99.8, "close": 100.0, "volume": 10.0} for _ in range(50)],
        index=live_module.pd.DatetimeIndex([start + live_module.pd.Timedelta(minutes=5 * i) for i in range(50)]),
    )

    for i in range(5000):
        ts = start + live_module.pd.Timedelta(minutes=5 * (50 + i))
        latest = live_module.pd.DataFrame(
            [{"open": 100.0, "high": 101.0, "low": 99.5, "close": 101.0 + (i * 0.001), "volume": 10.0}],
            index=live_module.pd.DatetimeIndex([ts]),
        )
        frame = live_module.pd.concat([frame, latest]).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = PaperLiveService._bound_frame(frame, 300)

    assert len(frame) <= 300
