from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

import main
from exchange.binance_spot_readonly_audit import (
    BinanceAuditConfigurationError,
    BinanceAuditRequestError,
    BinanceSpotReadOnlyAuditClient,
)


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


class _GetOnlyTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if not getattr(response, "preserve_url", False):
            response.url = url
        return response


class _Response:
    def __init__(self, payload, *, status_code=200, url="https://api.binance.com/api/v3/account", preserve_url=False):
        self.payload = payload
        self.status_code = status_code
        self.url = url
        self.preserve_url = preserve_url

    def json(self):
        return self.payload


def test_readonly_audit_client_uses_only_fixed_get_routes_and_signs_query():
    transport = _GetOnlyTransport([_Response({"balances": []})])
    client = BinanceSpotReadOnlyAuditClient(
        "audit-key", "audit-secret", transport=transport, clock_ms=lambda: 123456789
    )

    assert client.fetch_account() == {"balances": []}
    assert len(transport.calls) == 1
    url, request = transport.calls[0]
    assert url == "https://api.binance.com/api/v3/account"
    assert request["allow_redirects"] is False
    assert request["headers"] == {"X-MBX-APIKEY": "audit-key"}
    pairs = request["params"]
    signature = pairs[-1][1]
    unsigned = pairs[:-1]
    expected = hmac.new(b"audit-secret", urlencode(unsigned).encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "create_market_order")
    assert not hasattr(client, "cancel_order")
    assert not hasattr(client, "request")


def test_readonly_audit_client_rejects_generic_trade_credentials(monkeypatch):
    monkeypatch.setenv("AUDIT_BINANCE_READONLY_API_KEY", "same-key")
    monkeypatch.setenv("AUDIT_BINANCE_READONLY_SECRET", "same-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "same-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "same-secret")

    with pytest.raises(BinanceAuditConfigurationError, match="não pode ser"):
        BinanceSpotReadOnlyAuditClient.from_environment()


def test_readonly_audit_client_rejects_redirect_and_redacts_transport_error():
    redirect = _Response({}, status_code=302, url="https://evil.example/", preserve_url=True)
    transport = _GetOnlyTransport([redirect])
    client = BinanceSpotReadOnlyAuditClient("key-private", "secret-private", transport=transport)
    with pytest.raises(BinanceAuditRequestError, match="redirecionamento") as error:
        client.fetch_account()
    assert "secret-private" not in str(error.value)
    assert "key-private" not in str(error.value)

    class RaisingGet:
        def get(self, url, **kwargs):
            raise RuntimeError("leaked-url-signature-secret-private")

    safe_client = BinanceSpotReadOnlyAuditClient("key-private", "secret-private", transport=RaisingGet())
    with pytest.raises(BinanceAuditRequestError) as transport_error:
        safe_client.fetch_account()
    assert "secret-private" not in str(transport_error.value)
    assert "leaked-url" not in str(transport_error.value)


def test_readonly_audit_client_fetches_only_bnb_usdt_routes():
    transport = _GetOnlyTransport([
        _Response([]),
        _Response([]),
        _Response([]),
    ])
    client = BinanceSpotReadOnlyAuditClient("audit-key", "audit-secret", transport=transport, clock_ms=lambda: 1)
    since = int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp() * 1000)
    assert client.fetch_bnb_usdt_open_orders() == []
    assert client.fetch_bnb_usdt_orders(since) == []
    assert client.fetch_bnb_usdt_trades(since) == []
    assert [call[0].rsplit("/", 1)[-1] for call in transport.calls] == ["openOrders", "allOrders", "myTrades"]
    for _, request in transport.calls:
        params = dict(request["params"])
        assert params.get("symbol") == "BNBUSDT"
        assert "signature" in params


def test_readonly_audit_client_paginates_history_without_losing_rows():
    first_page = [{"orderId": index} for index in range(1000)]
    second_page = [{"orderId": 1000}]
    transport = _GetOnlyTransport([_Response(first_page), _Response(second_page)])
    client = BinanceSpotReadOnlyAuditClient("audit-key", "audit-secret", transport=transport, clock_ms=lambda: 1)

    rows = client.fetch_bnb_usdt_orders(100)

    assert len(rows) == 1001
    assert dict(transport.calls[0][1]["params"]).get("startTime") == 100
    assert dict(transport.calls[1][1]["params"]).get("orderId") == 1000
    assert "startTime" not in dict(transport.calls[1][1]["params"])


def test_readonly_audit_cli_fails_closed_without_dedicated_credentials(monkeypatch, capsys):
    from audit_binance_spot_readonly import main as audit_main

    monkeypatch.delenv("AUDIT_BINANCE_READONLY_API_KEY", raising=False)
    monkeypatch.delenv("AUDIT_BINANCE_READONLY_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "generic-key-must-not-be-used")
    monkeypatch.setenv("BINANCE_API_SECRET", "generic-secret-must-not-be-used")
    monkeypatch.setenv("AUDIT_DATABASE_URL", "mysql+pymysql://readonly@localhost/crypto_bot")
    monkeypatch.setattr("audit_binance_spot_readonly._read_mysql_snapshot",
                        lambda *_: pytest.fail("must stop before database and exchange requests"))
    args = ["--since", "2026-10-17T00:00:00Z", "--known-external-bnb", "0.00030331", "--confirm-read-only-credentials"]

    result = audit_main(args)
    output = capsys.readouterr().out
    assert result == 2
    report = json.loads(output)
    assert report["decision"] == "BLOQUEADO"
    assert "nenhuma chamada binance foi feita" in " ".join(report["blockers"]).lower()
    assert "generic-secret-must-not-be-used" not in output


def test_readonly_reconciliation_matches_balance_dust_fills_and_persisted_state(monkeypatch, tmp_path):
    from audit_binance_spot_readonly import reconcile

    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    state = [{
        "trade_id": 3081, "symbol": "BNB/USDT", "status": "DUST", "quantity": 0.00098726,
        "entry_fee_usdt": 0.00531328, "entry_fee_allocated_usdt": 0.00473,
        "realized_gross_pnl": -0.448098, "realized_exit_fees_usdt": 0.00437953,
        "realized_net_pnl": -0.457207, "fee_accounting_complete": True,
    }]
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    local_order = {
        "exchange_order_id": "1", "symbol": "BNB/USDT", "side": "SELL",
        "status": "CLOSED", "quantity": 0.008, "filled_quantity": 0.008,
        "fee": 0.00000599, "fee_currency": "BNB", "timestamp": datetime(2026, 10, 17, 1),
    }
    monkeypatch.setattr("audit_binance_spot_readonly._read_mysql_snapshot", lambda *_: {
        "snapshot_utc": "2026-10-17T02:00:00+00:00",
        "trade_3081": {
            "status": "DUST", "is_paper": 0, "symbol": "BNB/USDT", "quantity": 0.00098726,
            "pnl": -0.457207, "total_fees_usdt": 0.00969281, "exit_fee_usdt": 0.00437953,
            "entry_fee_usdt": 0.00531328, "entry_fee_allocated_usdt": 0.00473,
            "gross_pnl": -0.448098, "fee_accounting_complete": 1,
            "exit_price": None, "exit_reason": None, "exit_time": None,
        },
        "active_bnb": [{"id": 3081, "status": "DUST", "quantity": 0.00098726}],
        "live_eth": [{"id": 3080, "status": "OPEN", "quantity": 0.0038}],
        "local_orders": [local_order], "nonterminal_local_orders": 0, "bot_owned_bnb": 0.00098726,
    })
    api = _GetOnlyTransport([
        _Response({"balances": [
            {"asset": "BNB", "free": "0.00129057", "locked": "0"},
            {"asset": "USDT", "free": "28.26", "locked": "0"},
        ]}),
        _Response([]),
        _Response([{"orderId": 1, "symbol": "BNBUSDT", "side": "SELL", "status": "FILLED",
                    "origQty": "0.008", "executedQty": "0.008", "time": 1792200000000}]),
        _Response([{"id": 10, "orderId": 1, "symbol": "BNBUSDT", "isBuyer": False,
                    "price": "700", "qty": "0.008", "quoteQty": "5.6",
                    "commission": "0.00000599", "commissionAsset": "BNB", "time": 1792200000000}]),
    ])
    client = BinanceSpotReadOnlyAuditClient("dedicated-key", "dedicated-secret", transport=api, clock_ms=lambda: 1)

    report = reconcile(client, database_url="mysql+pymysql://readonly@localhost/crypto_bot",
                       state_path=state_path, since=since, known_external_bnb=0.00030331)

    assert report["decision"] == "LIBERADO PARA REINÍCIO"
    assert report["binance_assets"]["BNB"] == {"free": "0.00129057", "locked": "0"}
    assert report["usdt_free"] == pytest.approx(28.26)
    assert report["bnb_bot_owned_mysql"] == pytest.approx(0.00098726)
    assert report["trade_3081"]["mysql_status"] == report["trade_3081"]["json_status"] == "DUST"
    assert report["trade_3081"]["mysql_realized_pnl_usdt"] == pytest.approx(-0.457207)
    assert report["live_eth_positions_preserved"][0]["id"] == 3080
    assert len(api.calls) == 4


def test_readonly_reconciliation_blocks_unknown_fill(monkeypatch, tmp_path):
    from audit_binance_spot_readonly import reconcile

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps([{"trade_id": 3081, "symbol": "BNB/USDT", "status": "DUST",
                                       "quantity": 0.00098726, "realized_net_pnl": -0.457207}]), encoding="utf-8")
    monkeypatch.setattr("audit_binance_spot_readonly._read_mysql_snapshot", lambda *_: {
        "snapshot_utc": "2026-10-17T02:00:00+00:00",
        "trade_3081": {"status": "DUST", "is_paper": 0, "symbol": "BNB/USDT", "quantity": 0.00098726,
                        "pnl": -0.457207, "exit_price": None, "exit_reason": None, "exit_time": None},
        "active_bnb": [], "live_eth": [], "local_orders": [], "nonterminal_local_orders": 0,
        "bot_owned_bnb": 0.00098726,
    })
    api = _GetOnlyTransport([
        _Response({"balances": [{"asset": "BNB", "free": "0.00129057", "locked": "0"},
                                 {"asset": "USDT", "free": "28", "locked": "0"}]}),
        _Response([]),
        _Response([{"orderId": 999, "symbol": "BNBUSDT", "side": "SELL", "status": "FILLED"}]),
        _Response([]),
    ])
    report = reconcile(BinanceSpotReadOnlyAuditClient("dedicated-key", "dedicated-secret", transport=api),
                       database_url="mysql+pymysql://readonly@localhost/crypto_bot", state_path=state_path,
                       since=datetime(2026, 10, 17, tzinfo=timezone.utc), known_external_bnb=0.00030331)
    assert report["decision"] == "BLOQUEADO"
    assert report["unknown_exchange_order_ids"] == ["999"]
