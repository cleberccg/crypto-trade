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


def test_live_order_logs_distinguish_request_from_confirmed_fill(monkeypatch) -> None:
    import execution.order_executor as module

    session = _Session()
    monkeypatch.setattr(module, "get_session", lambda: session)
    messages: list[str] = []
    monkeypatch.setattr(module.logger, "warning", lambda template, *args: messages.append(template % args))
    monkeypatch.setattr(module.logger, "info", lambda template, *args: messages.append(template % args))
    exchange = _Exchange({
        "id": "ordem-42",
        "status": "closed",
        "info": {"fills": [{"price": "110", "qty": "0.004", "commission": "0.001", "commissionAsset": "USDT"}]},
    })

    result = OrderExecutor(exchange).execute_market_sell(
        SimpleNamespace(id=3081), "BNB/USDT", 0.004, 109.5
    )

    request = messages[0]
    confirmed = messages[-1]
    assert "ainda não confirmada" in request
    assert "trade_id=3081" in request and "symbol=BNB/USDT" in request
    assert "qty_solicitada=0.004000000000" in request
    assert "order_id=ordem-42" in confirmed
    assert "qty_executada=0.004000000000" in confirmed
    assert "preço_médio=110.0000000000" in confirmed
    assert "taxa_original=0.001000000000 USDT" in confirmed
    assert "taxa_usdt=0.0010000000 USDT" in confirmed
    assert "lado=VENDA" in confirmed
    assert "status=CLOSED" in confirmed and "Venda executada" in confirmed
    assert result.exchange_order_id == "ordem-42"
    assert all("api_key" not in message.lower() and "secret" not in message.lower() for message in messages)


def test_buy_order_logs_include_trade_order_fill_price_fee_and_status(monkeypatch) -> None:
    import execution.order_executor as module

    session = _Session()
    monkeypatch.setattr(module, "get_session", lambda: session)
    messages: list[str] = []
    monkeypatch.setattr(module.logger, "warning", lambda template, *args: messages.append(template % args))
    monkeypatch.setattr(module.logger, "info", lambda template, *args: messages.append(template % args))
    exchange = _Exchange({
        "id": "ordem-buy-43",
        "status": "closed",
        "info": {"fills": [{"price": "700", "qty": "0.01", "commission": "0.00001", "commissionAsset": "BNB"}]},
    })

    OrderExecutor(exchange).execute_market_buy(
        SimpleNamespace(id=3081), "BNB/USDT", 0.01, 699.5
    )

    assert "Solicitação de COMPRA" in messages[0]
    assert "execução ainda não confirmada" in messages[0]
    confirmed = messages[-1]
    assert "ORDEM_CONFIRMADA | lado=COMPRA" in confirmed
    assert "trade_id=3081" in confirmed and "order_id=ordem-buy-43" in confirmed
    assert "qty_solicitada=0.010000000000" in confirmed
    assert "qty_executada=0.010000000000" in confirmed
    assert "preço_médio=700.0000000000" in confirmed
    assert "taxa_original=0.000010000000 BNB" in confirmed
    assert "taxa_usdt=0.0070000000 USDT" in confirmed
    assert "status=CLOSED" in confirmed and "Compra executada" in confirmed


def test_central_formatter_localizes_risk_strategy_and_financial_outcomes() -> None:
    import logging

    from logging_service.formatter import build_formatter

    formatter = build_formatter()
    risk_record = logging.LogRecord(
        "risk.risk_manager", logging.INFO, "risk_manager.py", 10,
        "RiskManager input - portfolio=%.2f entry=%.6f stop=%s take=%s risk_pct=%.4f score=%.4f",
        (28.25, 721.14, "700", "800", 0.01, 1.0), None,
    )
    risk_text = formatter.format(risk_record)
    assert "[RISCO]" in risk_text
    assert "Avaliação de entrada pelo RiskManager" in risk_text
    assert "portfolio=28.25" in risk_text and "risk_pct=0.0100" in risk_text
    assert "INFO" in risk_text and "risk.risk_manager" in risk_text

    cases = [(-0.457207, "PREJUÍZO"), (0.25, "LUCRO"), (0.0, "NEUTRO")]
    for pnl, expected in cases:
        record = logging.LogRecord(
            "execution.live_trading_service", logging.INFO, "live_trading_service.py", 20,
            "LIVE_TRADE | trade_id=3081 status=DUST symbol=BNB/USDT quantity=0.000987260000 realized_pnl_usdt=%.6f",
            (pnl,), None,
        )
        rendered = formatter.format(record)
        assert "LIVE_TRADE |" in rendered
        assert "trade_id=3081" in rendered and "status=DUST" in rendered
        assert f"resultado={expected}" in rendered

    state = logging.LogRecord(
        "execution.live_trading_service", logging.INFO, "live_trading_service.py", 30,
        "LIVE_STATE | OPEN=0/3 DUST=1 DUST_qty=BNB:0.000987260000 capital_committed_usdt=0.00 dust_cost_usdt=0.777124 capital_free_usdt=28.25",
        (), None,
    )
    state_text = formatter.format(state)
    assert "LIVE_STATE |" in state_text and "status" not in state_text
    assert "leitura: posições OPEN 0/3; DUST 1" in state_text
    assert "USDT livre 28.25" in state_text
    assert "DUST não ocupa posição ativa" in state_text


def test_formatter_keeps_exception_names_and_legacy_monitor_markers() -> None:
    import logging

    from logging_service.formatter import build_formatter

    record = logging.LogRecord(
        "execution.order_executor", logging.INFO, "order_executor.py", 1,
        "ORDEM_CONFIRMADA | lado=BUY trade_id=17 order_id=abc symbol=BNB/USDT status=CLOSED | Compra executada",
        (), None,
    )
    rendered = build_formatter().format(record)
    assert "Compra executada" in rendered
    assert "trade_id=17" in rendered and "status=CLOSED" in rendered
    assert "order_executor" in rendered


def test_formatter_localizes_binance_and_market_data_but_keeps_api_fields() -> None:
    import logging

    from logging_service.formatter import build_formatter

    formatter = build_formatter()
    binance_record = logging.LogRecord(
        "exchange.binance_client", logging.INFO, "binance_client.py", 1,
        "create_market_order - symbol=%s side=%s qty=%s", ("BNB/USDT", "sell", 0.001), None,
    )
    binance_text = formatter.format(binance_record)
    assert "[BINANCE]" in binance_text
    assert "Solicitação de ordem a mercado" in binance_text
    assert "symbol=BNB/USDT side=sell qty=0.001" in binance_text

    daemon_record = logging.LogRecord(
        "market_data.daemon", logging.WARNING, "daemon.py", 2,
        "market-data-daemon retry symbol=%s tf=%s attempt=%d/%d delay=%.2fs error=%s",
        ("BNB/USDT", "4h", 2, 6, 4.0, "NetworkError"), None,
    )
    daemon_text = formatter.format(daemon_record)
    assert "[DADOS DE MERCADO]" in daemon_text
    assert "Nova tentativa de coleta" in daemon_text
    assert "symbol=BNB/USDT timeframe=4h tentativa=2/6" in daemon_text
    assert "NetworkError" in daemon_text


def test_formatter_preserves_signal_parser_contract_and_error_names() -> None:
    import logging

    from logging_service.formatter import build_formatter

    formatter = build_formatter()
    signal = logging.LogRecord(
        "execution.live_trading_service", logging.INFO, "live_trading_service.py", 1,
        "Sinal gerado - symbol=%s strategy=%s signal=%s score=%.4f price=%.6f reason=%s",
        ("BNB/USDT", "Sma200RegimeGated", "BUY", 0.9, 700.0, "conditions_met"), None,
    )
    signal_text = formatter.format(signal)
    assert "Sinal gerado" in signal_text and "signal=BUY" in signal_text

    failure = logging.LogRecord(
        "execution.live_trading_service", logging.ERROR, "live_trading_service.py", 2,
        "BUY blocked: unable to verify %s balance ownership. (%s)",
        ("BNB/USDT", "NetworkError"), None,
    )
    failure_text = formatter.format(failure)
    assert "COMPRA bloqueada:" in failure_text
    assert "BNB/USDT" in failure_text and "NetworkError" in failure_text


def test_formatter_labels_existing_closed_trade_and_paper_pnl_without_recalculation() -> None:
    import logging

    from logging_service.formatter import build_formatter

    formatter = build_formatter()
    closed_trade = logging.LogRecord(
        "execution.live_trading_service", logging.INFO, "live_trading_service.py", 1,
        "Posicao fechada - trade_id=%d gross_pnl=%.6f fees_usdt=%.6f net_pnl=%.6f",
        (3081, -0.448098, 0.009693, -0.457207), None,
    )
    closed_text = formatter.format(closed_trade)
    assert "trade_id=3081" in closed_text
    assert "net_pnl=-0.457207" in closed_text
    assert "resultado=PREJUÍZO" in closed_text

    paper = logging.LogRecord(
        "main", logging.INFO, "main.py", 2,
        "Paper trading complete - final=%.2f initial=%.2f PnL=%.2f (%.2f%%)",
        (9.0, 10.0, -1.0, -10.0), None,
    )
    paper_text = formatter.format(paper)
    assert "Paper trading concluído" in paper_text
    assert "PnL=-1.00" in paper_text and "resultado=PREJUÍZO" in paper_text
