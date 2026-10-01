from __future__ import annotations

import argparse

import pytest

import main


def test_live_parser_accepts_required_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "live",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--strategy-version",
            "v1.0",
            "--symbol",
            "BTC/USDT",
            "--timeframe",
            "15m",
        ],
    )

    args = main._parse_args()
    assert args.command == "live"
    assert args.strategy_name == "ClassicDonchianBreakout"
    assert args.strategy_version == "v1.0"
    assert args.symbol == "BTC/USDT"
    assert args.timeframe == "15m"


def test_live_parser_accepts_symbols_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "live",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--strategy-version",
            "v1.0",
            "--symbols",
            "BTC/USDT,ETH/USDT,SOL/USDT",
            "--timeframe",
            "15m",
        ],
    )

    args = main._parse_args()
    assert args.command == "live"
    assert args.symbols == "BTC/USDT,ETH/USDT,SOL/USDT"
    assert args.timeframe == "15m"


def test_live_parser_rejects_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["main.py", "live", "--strategy-name", "S"])
    with pytest.raises(SystemExit):
        main._parse_args()


def test_live_command_requires_explicit_real_order_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr("execution.live_trading_service.LiveTradingService", lambda **_: started.append(True))
    monkeypatch.setattr("exchange.binance_client.arm_real_orders", lambda: pytest.fail("must not arm"))
    args = argparse.Namespace(
        strategy_name="Sma200RegimeGated",
        strategy_version="v1",
        symbol="BNB/USDT",
        symbols="",
        timeframe="4h",
        enable_real_orders=False,
    )

    with pytest.raises(SystemExit, match="requires --enable-real-orders"):
        main.cmd_live(args)

    assert started == []


def test_emergency_close_parser_defaults_to_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "emergency-close", "--strategy-name", "Sma200RegimeGated", "--symbol", "BNB/USDT"],
    )

    args = main._parse_args()

    assert args.command == "emergency-close"
    assert args.enable_real_orders is False


def test_emergency_close_command_without_flag_does_not_arm_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, base_dir):
            captured["base_dir"] = base_dir

        def emergency_exit(self, **kwargs):
            captured["kwargs"] = kwargs
            return {
                "bot_position_quantity": 0.0,
                "current_price": 700.0,
                "estimated_notional": 0.0,
                "would_sell": False,
                "real_order_sent": False,
            }

    monkeypatch.setattr("execution.live_trading_service.LiveTradingService", _FakeService)
    monkeypatch.setattr("exchange.binance_client.arm_real_orders", lambda: pytest.fail("must not arm"))
    args = argparse.Namespace(strategy_name="Sma200RegimeGated", symbol="BNB/USDT", enable_real_orders=False)

    main.cmd_emergency_close(args)

    assert captured["kwargs"] == {
        "strategy_name": "Sma200RegimeGated",
        "symbol": "BNB/USDT",
        "enable_real_orders": False,
    }


def test_live_parser_rejects_capital_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "live",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--strategy-version",
            "v1.0",
            "--symbol",
            "BTC/USDT",
            "--timeframe",
            "15m",
            "--capital",
            "10000",
        ],
    )
    with pytest.raises(SystemExit):
        main._parse_args()


def test_cmd_live_creates_service_and_runs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, base_dir):
            captured["base_dir"] = base_dir

        def run(self, cfg):
            captured["cfg"] = cfg
            return {"status": "completed", "mode": "live"}

    monkeypatch.setattr("execution.live_trading_service.LiveTradingService", _FakeService)
    monkeypatch.setattr("exchange.binance_client.arm_real_orders", lambda: None)

    args = argparse.Namespace(
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        symbol="BTC/USDT",
        symbols="",
        timeframe="15m",
        poll_seconds=15.0,
        bootstrap_bars=1500,
        bootstrap_replay_bars=350,
        max_cycles=1,
        output_prefix="live",
        no_resume=False,
        enable_real_orders=True,
    )

    main.cmd_live(args)

    cfg = captured["cfg"]
    assert getattr(cfg, "strategy_name") == "ClassicDonchianBreakout"
    assert getattr(cfg, "strategy_version") == "v1.0"
    assert getattr(cfg, "symbol") == "BTC/USDT"
    assert tuple(getattr(cfg, "symbols")) == ("BTC/USDT",)
    assert getattr(cfg, "timeframe") == "15m"


def test_cmd_live_accepts_multi_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, base_dir):
            captured["base_dir"] = base_dir

        def run(self, cfg):
            captured["cfg"] = cfg
            return {"status": "completed", "mode": "live"}

    monkeypatch.setattr("execution.live_trading_service.LiveTradingService", _FakeService)
    monkeypatch.setattr("exchange.binance_client.arm_real_orders", lambda: None)

    args = argparse.Namespace(
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        symbol=None,
        symbols="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
        timeframe="15m",
        poll_seconds=15.0,
        bootstrap_bars=1500,
        bootstrap_replay_bars=350,
        max_cycles=1,
        output_prefix="live",
        no_resume=False,
        enable_real_orders=True,
    )

    main.cmd_live(args)

    cfg = captured["cfg"]
    assert getattr(cfg, "symbol") == "BTC/USDT"
    assert tuple(getattr(cfg, "symbols")) == (
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
    )
