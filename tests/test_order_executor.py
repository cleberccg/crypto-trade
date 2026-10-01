from __future__ import annotations

from types import SimpleNamespace

import pytest

from execution.order_executor import OrderExecutor


class _Session:
    def __init__(self) -> None:
        self.added = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def add(self, item) -> None:
        self.added.append(item)


class _Exchange:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.tickers: list[str] = []

    def create_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        return self.response

    def fetch_ticker(self, symbol: str) -> dict:
        self.tickers.append(symbol)
        return {"last": 3.0}


def _execute_buy(monkeypatch, response: dict):
    import execution.order_executor as module

    session = _Session()
    monkeypatch.setattr(module, "get_session", lambda: session)
    exchange = _Exchange(response)
    order = OrderExecutor(exchange).execute_market_buy(
        SimpleNamespace(id=1), "BNB/USDT", 0.01, 100.0
    )
    return order, session, exchange


def test_fee_in_usdt_uses_reported_cost_without_rate_assumption(monkeypatch) -> None:
    order, session, exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-usdt",
            "status": "closed",
            "info": {"fills": [{"price": "100", "qty": "0.01", "commission": "0.02", "commissionAsset": "USDT"}]},
        },
    )

    assert order.fee_usdt == 0.02
    assert order.fills[0].fee_cost == 0.02
    assert order.fills[0].fee_currency == "USDT"
    assert order.fills[0].fee_source == "REPORTED"
    assert exchange.tickers == []
    assert session.added[0].fee == 0.02


def test_fee_in_bnb_converts_at_fill_price_and_tracks_base_deduction(monkeypatch) -> None:
    order, session, exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-bnb",
            "status": "closed",
            "info": {"fills": [{"price": "700", "qty": "0.01", "commission": "0.00002", "commissionAsset": "BNB"}]},
        },
    )

    assert order.fee_usdt == 0.014
    assert order.base_fee_quantity == 0.00002
    assert order.fills[0].fee_conversion_price == 700.0
    assert exchange.tickers == []
    assert session.added[0].fee_currency == "BNB"


def test_other_fee_currency_converts_using_one_order_time_ticker(monkeypatch) -> None:
    order, _session, exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-other",
            "status": "closed",
            "info": {"fills": [
                {"price": "100", "qty": "0.004", "commission": "0.2", "commissionAsset": "FOO"},
                {"price": "101", "qty": "0.006", "commission": "0.2", "commissionAsset": "FOO"},
            ]},
        },
    )

    assert order.fee_usdt == pytest.approx(1.2)
    assert exchange.tickers == ["FOO/USDT"]


def test_multiple_fills_and_order_level_fees_are_allocated_once(monkeypatch) -> None:
    order, session, _exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-multi",
            "status": "closed",
            "filled": "0.01",
            "trades": [
                {"price": "100", "amount": "0.004"},
                {"price": "101", "amount": "0.006"},
            ],
            "fees": [{"cost": 0.03, "currency": "USDT"}],
        },
    )

    assert order.filled_quantity == 0.01
    assert order.price == pytest.approx(100.6)
    assert order.fee_usdt == pytest.approx(0.03)
    assert len(session.added) == 2
    assert sum(item.fee_usdt for item in session.added) == pytest.approx(0.03)
    assert sum(item.filled_quantity for item in session.added) == pytest.approx(0.01)


def test_multiple_binance_fill_fees_in_different_currencies_are_summed(monkeypatch) -> None:
    order, session, exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-fill-fees",
            "status": "closed",
            "info": {
                "fills": [
                    {"price": "100", "qty": "0.004", "commission": "0.004", "commissionAsset": "USDT"},
                    {"price": "101", "qty": "0.006", "commission": "0.00001", "commissionAsset": "BNB"},
                ]
            },
        },
    )

    assert order.filled_quantity == pytest.approx(0.01)
    assert order.fee_usdt == pytest.approx(0.00501)
    assert order.fee_source == "REPORTED"
    assert order.fee_currency == "MULTIPLE"
    assert exchange.tickers == []
    assert [item.fee_currency for item in session.added] == ["USDT", "BNB"]
    assert sum(item.filled_quantity for item in session.added) == pytest.approx(0.01)


def test_order_level_fee_fills_only_missing_per_fill_fee_without_double_count(monkeypatch) -> None:
    order, session, _exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-mixed-fees",
            "status": "closed",
            "fees": [{"cost": 0.01, "currency": "USDT"}],
            "info": {"fills": [
                {"price": "100", "qty": "0.004", "commission": "0.004", "commissionAsset": "USDT"},
                {"price": "101", "qty": "0.006"},
            ]},
        },
    )

    assert order.fee_usdt == pytest.approx(0.01)
    assert [item.fee_usdt for item in session.added] == pytest.approx([0.004, 0.006])
    assert session.added[1].fee_source == "ORDER_ALLOCATED"


def test_fee_missing_is_marked_missing_not_estimated(monkeypatch) -> None:
    order, session, _exchange = _execute_buy(
        monkeypatch,
        {
            "id": "order-no-fee",
            "status": "closed",
            "filled": "0.01",
            "average": "100",
        },
    )

    assert order.filled_quantity == 0.01
    assert order.fee_usdt is None
    assert order.fee_source == "MISSING"
    assert session.added[0].fee == 0.0
    assert session.added[0].fee_source == "MISSING"


def test_zero_fill_does_not_use_requested_quantity(monkeypatch) -> None:
    order, session, _exchange = _execute_buy(
        monkeypatch,
        {"id": "order-zero", "status": "canceled", "filled": "0", "amount": "0.01"},
    )

    assert order.quantity == 0.01
    assert order.filled_quantity == 0.0
    assert order.fills[0].fee_source == "MISSING"
    assert session.added[0].filled_quantity == 0.0


def test_market_sell_persists_fill_and_reported_fee(monkeypatch) -> None:
    import execution.order_executor as module

    session = _Session()
    monkeypatch.setattr(module, "get_session", lambda: session)
    exchange = _Exchange({
        "id": "sell-1",
        "status": "closed",
        "info": {"fills": [{"price": "110", "qty": "0.004", "commission": "0.001", "commissionAsset": "USDT"}]},
    })
    order = OrderExecutor(exchange).execute_market_sell(
        SimpleNamespace(id=2), "BNB/USDT", 0.004, 110.0
    )

    assert order.filled_quantity == pytest.approx(0.004)
    assert order.price == pytest.approx(110.0)
    assert order.fee_usdt == pytest.approx(0.001)
    assert session.added[0].side == "SELL"
    assert session.added[0].fee_currency == "USDT"


def test_total_reported_fee_sums_exchange_fee_rows() -> None:
    fee = OrderExecutor._total_reported_fee(
        {"fees": [{"cost": 0.01}, {"cost": 0.02}], "fee": {"cost": 0.5}}
    )

    assert fee == 0.03


def test_total_reported_fee_uses_single_fee_when_fee_list_empty() -> None:
    fee = OrderExecutor._total_reported_fee({"fees": [], "fee": {"cost": 0.01}})

    assert fee == 0.01
