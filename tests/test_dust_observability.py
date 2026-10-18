from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import audit_live_readiness
import monitor_live_trades
from database.models import Trade
from execution.live_trading_service import _LivePositionStore


def _observability_db():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol VARCHAR(20), strategy_name VARCHAR(100), "
            "timeframe VARCHAR(10), side VARCHAR(5), status VARCHAR(10) NOT NULL, "
            "is_paper BOOLEAN NOT NULL, entry_price FLOAT NOT NULL, exit_price FLOAT, "
            "quantity FLOAT NOT NULL, stake_amount FLOAT NOT NULL, pnl FLOAT, fee FLOAT NOT NULL, "
            "entry_time DATETIME NOT NULL, exit_time DATETIME, exit_reason VARCHAR(50))"
        ))
        connection.execute(text(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, trade_id INTEGER, symbol VARCHAR(20), "
            "side VARCHAR(5), status VARCHAR(20), order_type VARCHAR(20), price FLOAT, "
            "quantity FLOAT, filled_quantity FLOAT, fee FLOAT, exchange_order_id VARCHAR(64), timestamp DATETIME)"
        ))
    return engine


def _insert_trade(connection, *, trade_id, symbol, status, quantity, entry_price,
                  is_paper=False, pnl=None, fee=0.0):
    connection.execute(text(
        "INSERT INTO trades (id,symbol,strategy_name,timeframe,side,status,is_paper,entry_price,"
        "quantity,stake_amount,pnl,fee,entry_time) VALUES "
        "(:id,:symbol,'Sma200RegimeGated','4h','BUY',:status,:is_paper,:entry_price,"
        ":quantity,:stake,:pnl,:fee,:entry_time)"
    ), {
        "id": trade_id,
        "symbol": symbol,
        "status": status,
        "is_paper": is_paper,
        "entry_price": entry_price,
        "quantity": quantity,
        "stake": quantity * entry_price,
        "pnl": pnl,
        "fee": fee,
        "entry_time": datetime(2026, 10, 4, tzinfo=timezone.utc),
    })


def test_monitor_reports_dust_without_counting_it_open_or_closed(monkeypatch, capsys):
    engine = _observability_db()
    with engine.begin() as connection:
        _insert_trade(connection, trade_id=3081, symbol="BNB/USDT", status="DUST",
                      quantity=0.00098726, entry_price=787.152,
                      pnl=-0.457207, fee=0.00969281)
        _insert_trade(connection, trade_id=4000, symbol="BNB/USDT", status="OPEN",
                      quantity=0.01, entry_price=700.0)
        _insert_trade(connection, trade_id=9, symbol="BTC/USDT", status="CLOSED",
                      quantity=0.0, entry_price=100.0, pnl=2.0, fee=0.1)
        _insert_trade(connection, trade_id=10, symbol="ETH/USDT", status="CANCELLED",
                      quantity=0.0, entry_price=100.0)

    monkeypatch.setattr(monitor_live_trades, "get_db", lambda: SimpleNamespace(engine=engine))
    report = monitor_live_trades._poll_database()

    assert report["open_trades"] == 1
    assert report["dust_trades"] == 1
    assert report["closed_trades"] == 1
    assert report["other_trades"] == 1
    assert report["total_trades"] == 4
    dust = report["dust_positions"][0]
    assert dust["trade_id"] == 3081
    assert dust["symbol"] == "BNB/USDT"
    assert dust["ownership"] == "BOT_LIVE"
    assert dust["quantity"] == pytest.approx(0.00098726)
    assert dust["residual_cost_usdt"] == pytest.approx(0.00098726 * 787.152)
    assert dust["realized_pnl_usdt"] == pytest.approx(-0.457207)
    assert dust["recorded_fees_usdt"] == pytest.approx(0.00969281)
    assert "closed_pnl" not in report

    monitor_live_trades._report_database_state(monitor_live_trades.MonitorState(), report)
    output = capsys.readouterr().out
    assert "open=1 dust=1 closed=1" in output
    assert "trade_id=3081 symbol=BNB/USDT qty=0.000987260000" in output
    assert "owner=BOT_LIVE" in output
    assert "realized_pnl_usdt=-0.45720700" in output
    engine.dispose()


def test_audit_includes_live_dust_aggregates_ownership_and_leaves_surplus_unassigned(monkeypatch):
    engine = _observability_db()
    with engine.begin() as connection:
        _insert_trade(connection, trade_id=3081, symbol="BNB/USDT", status="DUST",
                      quantity=0.00098726, entry_price=787.152,
                      pnl=-0.457207, fee=0.00969281)
        _insert_trade(connection, trade_id=3082, symbol="BNB/USDT", status="DUST",
                      quantity=0.001, entry_price=700.0)
        _insert_trade(connection, trade_id=4000, symbol="BNB/USDT", status="OPEN",
                      quantity=0.01, entry_price=700.0)
        _insert_trade(connection, trade_id=12, symbol="BNB/USDT", status="OPEN",
                      quantity=0.5, entry_price=700.0, is_paper=True)
        _insert_trade(connection, trade_id=13, symbol="BTC/USDT", status="CLOSED",
                      quantity=0.0, entry_price=100.0)
        connection.execute(text(
            "INSERT INTO orders (id,trade_id,side,status,price,quantity,filled_quantity,fee,timestamp) "
            "VALUES (1,3081,'SELL','FILLED',721.14,0.008,0.008,0.004,:ts)"
        ), {"ts": datetime(2026, 10, 4, tzinfo=timezone.utc)})

    @contextmanager
    def isolated_session():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(audit_live_readiness, "get_session", isolated_session)

    class NoExchangeCalls:
        def __getattr__(self, name):
            raise AssertionError(f"Exchange access is not expected: {name}")

    total_bot_qty = 0.00098726 + 0.001 + 0.01
    balance = {"total": {"BNB": total_bot_qty + 0.00030331}}
    ownership = audit_live_readiness.audit_live_ownership(balance)
    positions = ownership["positions"]
    dust = [position for position in positions if position["status"] == "DUST"]
    assert {position["trade_id"] for position in dust} == {3081, 3082}
    assert len(positions) == 3  # paper OPEN e CLOSED não são ownership LIVE
    first = next(position for position in dust if position["trade_id"] == 3081)
    assert first["ownership"] == "BOT_LIVE"
    assert first["residual_quantity"] == pytest.approx(0.00098726)
    assert first["residual_cost_usdt"] == pytest.approx(0.00098726 * 787.152)
    assert first["realized_pnl_usdt"] == pytest.approx(-0.457207)
    assert first["recorded_fees_usdt"] == pytest.approx(0.00969281)
    assert len(first["orders"]) == 1

    asset = ownership["assets"][0]
    assert asset["bot_owned_quantity"] == pytest.approx(total_bot_qty)
    assert asset["unassigned_quantity"] == pytest.approx(0.00030331)
    assert asset["ownership_status"] == "UNASSIGNED_SURPLUS"
    assert first["ownership_status"] == "UNASSIGNED_SURPLUS"

    # Saldo menor que a soma de OPEN+DUST é divergência; saldo excedente nunca é adotado.
    shortage = audit_live_readiness.audit_live_ownership({"total": {"BNB": total_bot_qty - 0.002}})
    assert shortage["assets"][0]["ownership_status"] == "OWNERSHIP_SHORTFALL"
    assert shortage["assets"][0]["shortfall_quantity"] == pytest.approx(0.002)
    compatibility_rows = audit_live_readiness.audit_open_db_positions(NoExchangeCalls(), balance)
    assert len(compatibility_rows) == 1
    assert compatibility_rows[0]["status"] == "OPEN"
    engine.dispose()


def test_model_and_legacy_state_remain_compatible_with_dust(tmp_path: Path):
    assert Trade.__table__.c.status.type.length == 10

    legacy_state = {
        "trade_id": 77,
        "symbol": "BNB/USDT",
        "quantity": 0.002,
        "entry_price": 700.0,
        "stop_loss": 680.0,
        "take_profit": 740.0,
    }
    state_path = tmp_path / "live_positions.json"
    state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
    restored = _LivePositionStore(state_path).load_all()

    assert len(restored) == 1
    assert restored[0].trade_id == 77
    assert restored[0].status == "OPEN"
    assert restored[0].quantity == pytest.approx(0.002)
    assert restored[0].realized_net_pnl is None


def test_dry_run_cannot_cancel_an_exchange_order():
    exchange = audit_live_readiness.DryRunExchange()
    with pytest.raises(RuntimeError, match="not allowed in dry run"):
        exchange.cancel_order("fixture-order", "BNB/USDT")