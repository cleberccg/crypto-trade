from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

from monitoring.asset_ranking import AssetRankingMonitor


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query, _params=None):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Result(self._rows)


def _session_factory(rows):
    @contextmanager
    def _factory():
        yield _FakeSession(rows)

    return _factory


def test_asset_ranking_observacao_below_minimum_trades() -> None:
    base = datetime(2026, 7, 1, 0, 0, 0)
    rows = [
        ("ETH/USDT", base + timedelta(minutes=idx), 0.10 if idx % 2 == 0 else -0.05)
        for idx in range(10)
    ]
    monitor = AssetRankingMonitor(
        session_factory=_session_factory(rows),
        min_trades_per_symbol=30,
    )

    ranking = monitor.snapshot(strategy_name="ClassicDonchianBreakout")
    assert len(ranking) == 1
    assert ranking[0].symbol == "ETH/USDT"
    assert ranking[0].trades == 10
    assert ranking[0].status == "OBSERVACAO"


def test_asset_ranking_classification_after_minimum_trades() -> None:
    base = datetime(2026, 7, 1, 0, 0, 0)
    rows = []

    # EXCELENTE: PF > 1.5
    for idx in range(30):
        pnl = 0.20 if idx < 20 else -0.10
        rows.append(("ETH/USDT", base + timedelta(minutes=idx), pnl))

    # BOM: 1.2 <= PF <= 1.5
    for idx in range(30):
        pnl = 0.13 if idx < 16 else -0.10
        rows.append(("BNB/USDT", base + timedelta(minutes=100 + idx), pnl))

    # OBSERVACAO: 0.9 <= PF < 1.2
    for idx in range(30):
        pnl = 0.10 if idx < 15 else -0.10
        rows.append(("SOL/USDT", base + timedelta(minutes=200 + idx), pnl))

    # SUSPENDER: PF < 0.9
    for idx in range(30):
        pnl = 0.10 if idx < 10 else -0.10
        rows.append(("BTC/USDT", base + timedelta(minutes=300 + idx), pnl))

    monitor = AssetRankingMonitor(
        session_factory=_session_factory(rows),
        min_trades_per_symbol=30,
    )

    ranking = monitor.snapshot(strategy_name="ClassicDonchianBreakout")
    by_symbol = {item.symbol: item for item in ranking}

    assert by_symbol["ETH/USDT"].status == "EXCELENTE"
    assert by_symbol["BNB/USDT"].status == "BOM"
    assert by_symbol["SOL/USDT"].status == "OBSERVACAO"
    assert by_symbol["BTC/USDT"].status == "SUSPENDER"

    assert by_symbol["ETH/USDT"].profit_factor > 1.50
    assert 1.20 <= by_symbol["BNB/USDT"].profit_factor <= 1.50
    assert 0.90 <= by_symbol["SOL/USDT"].profit_factor < 1.20
    assert by_symbol["BTC/USDT"].profit_factor < 0.90


# Exercise the LIVE scheduling path with existing exchange/store/risk mocks.
def test_live_ranking_refreshes_every_twenty_cycles(monkeypatch, caplog):
    from dataclasses import replace
    from tests.test_live_execution_cycle import (
        _service, _cfg, _candle, _ScriptedExchange, _ScriptedStrategy,
        _FakeLRS, _FakeOE, _InMemoryPositionStore,
    )
    from strategies.base_strategy import SignalType
    import execution.live_trading_service as live

    class Monitor:
        def __init__(self): self.calls = []
        def snapshot(self, strategy_name=None):
            self.calls.append(strategy_name)
            return []

    monkeypatch.setattr(live.settings, "trading", replace(
        live.settings.trading, asset_ranking_refresh_cycles=20))
    monitor = Monitor()
    service = _service(_ScriptedExchange(candles=[_candle(100, 0)]),
                       _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(),
                       _InMemoryPositionStore())
    service._asset_ranking_monitor_factory = lambda **kwargs: monitor
    caplog.set_level("INFO", logger="execution.live_trading_service")
    # Reuse the same service so each run also proves the actual cycle boundaries.
    for cycles, expected in [(19, 0), (20, 1), (39, 2), (40, 4)]:
        service._exchange_factory = lambda: _ScriptedExchange(candles=[_candle(100, 0)])
        service.run(_cfg(cycles))
        assert len(monitor.calls) == expected
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Sem trades CLOSED para ClassicDonchianBreakout") == 1


def test_ranking_empty_message_only_on_first_occurrence_and_transition(caplog):
    from pathlib import Path
    from execution.live_trading_service import LiveTradingService

    class Monitor:
        rows = []
        def snapshot(self, strategy_name=None): return self.rows

    service = LiveTradingService(base_dir=Path("."))
    monitor = Monitor()
    caplog.set_level("INFO", logger="execution.live_trading_service")
    for _ in range(3): service._log_asset_ranking(monitor, "Sma200RegimeGated")
    # Use a real ranking result to preserve the existing row/calc contract.
    monitor.rows = AssetRankingMonitor(session_factory=_session_factory([
        ("BNB/USDT", datetime(2026, 10, 17), -0.5)
    ])).snapshot("Sma200RegimeGated")
    service._log_asset_ranking(monitor, "Sma200RegimeGated")
    monitor.rows = []
    for _ in range(3): service._log_asset_ranking(monitor, "Sma200RegimeGated")
    service._log_asset_ranking(monitor, "OtherStrategy")
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Sem trades CLOSED para Sma200RegimeGated") == 2
    assert messages.count("Sem trades CLOSED para OtherStrategy") == 1
    assert any("Ranking ativos (estrategia=Sma200RegimeGated)" in m for m in messages)


def test_ranking_error_does_not_reset_empty_state(caplog):
    from pathlib import Path
    from execution.live_trading_service import LiveTradingService

    class Monitor:
        failed = False
        def snapshot(self, strategy_name=None):
            if self.failed: raise RuntimeError("fixture failure")
            return []

    service = LiveTradingService(base_dir=Path("."))
    monitor = Monitor()
    caplog.set_level("INFO", logger="execution.live_trading_service")
    service._log_asset_ranking(monitor, "Sma200RegimeGated")
    monitor.failed = True
    service._log_asset_ranking(monitor, "Sma200RegimeGated")
    monitor.failed = False
    service._log_asset_ranking(monitor, "Sma200RegimeGated")
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Sem trades CLOSED para Sma200RegimeGated") == 1
    assert any("Falha ao atualizar ranking" in m for m in messages)


def test_ranking_keeps_closed_and_strategy_filters_excluding_realized_dust():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE trades (id INTEGER, symbol TEXT, exit_time TEXT, pnl FLOAT, status TEXT, strategy_name TEXT)"))
            connection.execute(text(
                "INSERT INTO trades VALUES "
                "(1,'BNB/USDT',NULL,-0.457207,'DUST','Sma200RegimeGated'),"
                "(2,'BNB/USDT','2026-10-17',1.0,'CLOSED','OtherStrategy'),"
                "(3,'BNB/USDT','2026-10-17',0.25,'CLOSED','Sma200RegimeGated')"
            ))
        monitor = AssetRankingMonitor(session_factory=engine.connect)
        rows = monitor.snapshot("Sma200RegimeGated")
        assert len(rows) == 1
        assert rows[0].trades == 1
        assert rows[0].net_profit == 0.25
    finally:
        engine.dispose()
