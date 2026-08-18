from __future__ import annotations

from decimal import Decimal

from paper_trading.paper_broker import PaperBroker
from risk.portfolio_value_provider import BinancePortfolioValueProvider, PaperPortfolioValueProvider


class _FakeExchange:
    def __init__(self, balance: dict) -> None:
        self._balance = balance

    def fetch_balance(self) -> dict:
        return dict(self._balance)


class TestBinancePortfolioValueProvider:
    def test_uses_free_usdt_when_locked_is_zero(self) -> None:
        provider = BinancePortfolioValueProvider(
            _FakeExchange(
                {
                    "free": {"USDT": 100},
                    "locked": {"USDT": 0},
                }
            )
        )
        assert provider.get_available_portfolio_value() == Decimal("100")

    def test_uses_only_free_usdt_when_locked_exists(self) -> None:
        provider = BinancePortfolioValueProvider(
            _FakeExchange(
                {
                    "free": {"USDT": 35},
                    "locked": {"USDT": 65},
                    "used": {"USDT": 65},
                    "total": {"USDT": 100},
                }
            )
        )
        assert provider.get_available_portfolio_value() == Decimal("35")

    def test_uses_free_usdt_even_when_locked_is_high(self) -> None:
        provider = BinancePortfolioValueProvider(
            _FakeExchange(
                {
                    "free": {"USDT": 2},
                    "locked": {"USDT": 98},
                    "total": {"USDT": 100},
                }
            )
        )
        assert provider.get_available_portfolio_value() == Decimal("2")

    def test_locked_never_enters_calculation(self) -> None:
        provider = BinancePortfolioValueProvider(
            _FakeExchange(
                {
                    "free": {"USDT": 7},
                    "locked": {"USDT": 999999},
                    "used": {"USDT": 999999},
                    "total": {"USDT": 1000006},
                }
            )
        )
        assert provider.get_available_portfolio_value() == Decimal("7")


class TestPaperPortfolioValueProvider:
    def test_returns_paper_cash_balance(self) -> None:
        broker = PaperBroker(initial_capital=1234.5)
        provider = PaperPortfolioValueProvider(broker)
        assert provider.get_available_portfolio_value() == Decimal("1234.5")
