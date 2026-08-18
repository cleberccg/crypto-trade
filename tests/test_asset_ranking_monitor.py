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
