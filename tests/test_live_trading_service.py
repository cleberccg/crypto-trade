from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from execution.live_trading_service import LiveTradingConfig, LiveTradingService
from strategies.base_strategy import SignalType, StrategySignal


class _FakeStrategy:
    def initialize(self) -> None:
        return None


class _GateAwareStrategy:
    def __init__(self, *, weight: float = 1.0) -> None:
        self.weight = float(weight)

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        price = float(df["close"].iloc[-1])
        ts = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else datetime.now(tz=timezone.utc)
        return StrategySignal(
            signal=SignalType.BUY,
            price=price,
            timestamp=ts,
            score=self.weight,
            stop_loss=price * 0.99,
            take_profit=price * 1.02,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        price = float(df["close"].iloc[-1])
        ts = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else datetime.now(tz=timezone.utc)
        return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=ts)


class _FakeExchange:
    def __init__(self, free_usdt: float, fail_balance: bool = False) -> None:
        self.free_usdt = free_usdt
        self.fail_balance = fail_balance
        self.connected = False
        self.disconnected = False
        self.fetch_balance_calls = 0
        self.fetch_ohlcv_calls = 0
        self.fetch_ticker_calls = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def fetch_balance(self) -> dict:
        self.fetch_balance_calls += 1
        if self.fail_balance:
            raise RuntimeError("api_down")
        return {
            "free": {"USDT": self.free_usdt},
            "locked": {"USDT": max(0.0, 100.0 - self.free_usdt)},
            "total": {"USDT": 100.0},
        }

    def fetch_ohlcv(self, symbol: str, timeframe: str, since=None, limit=None):
        self.fetch_ohlcv_calls += 1
        if self.fetch_ohlcv_calls > 1:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ts = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        return pd.DataFrame(
            [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}],
            index=pd.DatetimeIndex([ts]),
        )

    def fetch_ticker(self, symbol: str) -> dict:
        self.fetch_ticker_calls += 1
        return {"last": 100.5}

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return {"bids": [], "asks": []}

    def create_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        return {"id": "1", "status": "closed", "filled": quantity, "price": 100.0}

    def create_limit_order(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        return {"id": "2", "status": "open"}

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        return {"id": order_id, "status": "canceled"}

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return {"id": order_id, "status": "closed"}

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        return []


class _SpySizer:
    def __init__(self) -> None:
        self.portfolio_values: list[float] = []

    def fixed_fractional(self, portfolio_value: float, stake_pct: float, price: float) -> float:
        return 0.0

    def risk_based(self, portfolio_value: float, risk_pct: float, entry_price: float, stop_loss_price: float) -> float:
        self.portfolio_values.append(float(portfolio_value))
        max_loss = float(portfolio_value) * float(risk_pct)
        risk_per_unit = float(entry_price) - float(stop_loss_price)
        return max_loss / risk_per_unit


def _cfg(max_cycles: int = 1) -> LiveTradingConfig:
    return LiveTradingConfig(
        symbol="BTC/USDT",
        timeframe="15m",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        poll_seconds=0.01,
        bootstrap_bars=200,
        bootstrap_replay_bars=100,
        max_cycles=max_cycles,
        resume=True,
        output_prefix="live",
    )


def _db_stub() -> dict:
    return {
        "load_all_open_trade_states": lambda: {},
        "load_trade_state": lambda **kw: None,
        "is_trade_open": lambda **kw: False,
    }


def test_reads_free_balance_on_startup() -> None:
    exchange = _FakeExchange(free_usdt=35.0)
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _FakeStrategy(),
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )

    result = service.run(_cfg(max_cycles=1))

    assert result["free_usdt"] == pytest.approx(35.0)
    assert exchange.fetch_balance_calls >= 1


def test_blocks_when_free_balance_is_zero() -> None:
    exchange = _FakeExchange(free_usdt=0.0)
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _FakeStrategy(),
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )

    with pytest.raises(RuntimeError, match="Saldo USDT livre indisponivel"):
        service.run(_cfg(max_cycles=1))


def test_position_sizer_receives_exact_provider_value() -> None:
    exchange = _FakeExchange(free_usdt=123.45)
    spy = _SpySizer()
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _FakeStrategy(),
        sizer_factory=lambda: spy,
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )

    service.run(_cfg(max_cycles=1))

    assert spy.portfolio_values
    assert spy.portfolio_values[0] == pytest.approx(123.45)


def test_full_initialization_flow_calls_dependencies() -> None:
    exchange = _FakeExchange(free_usdt=100.0)
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _FakeStrategy(),
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )

    result = service.run(_cfg(max_cycles=1))

    assert result["status"] == "completed"
    assert exchange.connected is True
    assert exchange.disconnected is True
    assert exchange.fetch_balance_calls >= 1
    assert exchange.fetch_ohlcv_calls >= 1
    assert exchange.fetch_ticker_calls >= 1


def test_api_failure_aborts_execution() -> None:
    exchange = _FakeExchange(free_usdt=100.0, fail_balance=True)
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _FakeStrategy(),
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )

    with pytest.raises(RuntimeError, match="api_down"):
        service.run(_cfg(max_cycles=1))


def test_live_runtime_strategy_uses_hypothesis_payload_for_params_and_gate() -> None:
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: _FakeExchange(free_usdt=100.0),
        strategy_factory=lambda _name, **kwargs: _GateAwareStrategy(**kwargs),
        sleep_fn=lambda _: None,
        db_ops=_db_stub(),
    )
    cfg = LiveTradingConfig(
        symbol="BTC/USDT",
        timeframe="5m",
        strategy_name="AnyStrategy",
        strategy_version="v1.0",
        max_cycles=1,
        hypothesis_config={
            "approved_parameters": {"weight": 0.77},
            "approved_filters": ["gate_flag >= 1"],
            "regime": "bullish|high_volatility",
            "approved_contexts": [
                {
                    "symbol": "BTC/USDT",
                    "timeframe": "5m",
                    "trend_bucket": "bullish",
                    "vol_regime": "high_volatility",
                }
            ],
        },
    )

    strategy = service._build_runtime_strategy(cfg, symbol="BTC/USDT", timeframe="5m")

    idx = pd.DatetimeIndex([datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)])
    df_ok = pd.DataFrame(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0, "gate_flag": 1.0, "trend_bucket": "bullish", "vol_regime": "high_volatility"}],
        index=idx,
    )
    df_blocked = pd.DataFrame(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0, "gate_flag": 0.0, "trend_bucket": "bullish", "vol_regime": "high_volatility"}],
        index=idx,
    )

    sig_ok = strategy.entry_signal(df_ok)
    sig_blocked = strategy.entry_signal(df_blocked)

    assert isinstance(strategy._base, _GateAwareStrategy)
    assert strategy._base.weight == pytest.approx(0.77)
    assert sig_ok.signal == SignalType.BUY
    assert sig_blocked.signal == SignalType.HOLD
