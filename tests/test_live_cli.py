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
