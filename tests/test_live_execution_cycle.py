"""
Tests for the full live execution cycle implemented in LiveTradingService.

Coverage:
- test_reentry_blocked_while_position_open
- test_full_buy_then_stop_loss_closes_position
- test_full_buy_then_take_profit_closes_position
- test_binance_order_rejection_continues_loop
- test_restart_with_open_position_reconciles_from_state_file
- test_idempotency_skips_buy_when_open_orders_exist
- test_reconciliation_on_startup_no_open_position
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
import pytest
import execution.live_trading_service as live_service_module

from execution.live_trading_service import (
    LivePositionState,
    LiveTradingConfig,
    LiveTradingService,
)
from strategies.base_strategy import SignalType, StrategySignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _InMemoryPositionStore:
    """In-memory substitute for _LivePositionStore."""

    def __init__(self, initial_state: LivePositionState | None = None, initial_states: list[LivePositionState] | None = None) -> None:
        self._states: list[LivePositionState] = list(initial_states or ([] if initial_state is None else [initial_state]))
        self.saved: list[LivePositionState] = []
        self.cleared: int = 0

    def save(self, state: LivePositionState) -> None:
        self._states = [state]
        self.saved.append(state)

    def save_all(self, states: list[LivePositionState]) -> None:
        self._states = list(states)
        self.saved.extend(states)

    def load(self) -> LivePositionState | None:
        return self._states[0] if self._states else None

    def load_all(self) -> list[LivePositionState]:
        return list(self._states)

    def clear(self) -> None:
        self._states = []
        self.cleared += 1


class _Obj:
    """Generic attribute namespace for faking ORM/dataclass objects."""
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _candle(close: float, hour: int = 0) -> pd.DataFrame:
    ts = datetime(2026, 7, 9, hour, 0, tzinfo=timezone.utc)
    return pd.DataFrame(
        [{"open": close * 0.999, "high": close * 1.001,
          "low": close * 0.998, "close": close, "volume": 10.0}],
        index=pd.DatetimeIndex([ts]),
    )


class _ScriptedExchange:
    """Exchange fake that serves a scripted sequence of candles."""

    def __init__(
        self,
        free_usdt: float = 100.0,
        candles: list[pd.DataFrame] | None = None,
        candles_by_symbol: dict[str, list[pd.DataFrame]] | None = None,
        open_orders: list[dict] | None = None,
    ) -> None:
        self.free_usdt = free_usdt
        self._candles = list(candles or [])
        self._idx = 0
        self._candles_by_symbol = {
            str(symbol): list(series)
            for symbol, series in (candles_by_symbol or {}).items()
        }
        self._idx_by_symbol = {str(symbol): 0 for symbol in self._candles_by_symbol}
        self._open_orders = list(open_orders or [])
        self.connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def fetch_balance(self) -> dict:
        return {"free": {"USDT": self.free_usdt}}

    def fetch_ohlcv(self, symbol: str, timeframe: str, since=None, limit=None):
        symbol_key = str(symbol)
        if symbol_key in self._candles_by_symbol:
            series = self._candles_by_symbol[symbol_key]
            idx = self._idx_by_symbol[symbol_key]
            if idx >= len(series):
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = series[idx]
            self._idx_by_symbol[symbol_key] = idx + 1
            return df

        if self._idx >= len(self._candles):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = self._candles[self._idx]
        self._idx += 1
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": 100.0}

    def fetch_open_orders(self, symbol=None) -> list:
        return list(self._open_orders)

    def create_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        return {
            "id": "fake_market_order",
            "status": "closed",
            "filled": quantity,
            "average": 100.0,
            "price": 100.0,
            "fee": {"cost": 0.0},
        }


class _FakeLRS:
    """Fake LiveRiskService — records calls, optionally raises."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._exc = exc

    def execute_market_buy_with_risk(self, trade, symbol, entry_price,
                                     stop_loss, take_profit, **kw):
        if self._exc is not None:
            raise self._exc
        self.calls.append({"symbol": symbol, "entry_price": entry_price})
        order = _Obj(price=entry_price, filled_quantity=0.001,
                     exchange_order_id="fake_buy_1")
        risk_params = _Obj(quantity=0.001, stake_amount=0.1)
        return _Obj(order=order, risk_params=risk_params, portfolio_value=100.0)


class _FakeOE:
    """Fake OrderExecutor — records sell calls, optionally raises."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.sell_calls: list[dict] = []
        self._exc = exc

    def execute_market_sell(self, trade, symbol, quantity, price):
        if self._exc is not None:
            raise self._exc
        self.sell_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        return _Obj(price=price, filled_quantity=quantity,
                    exchange_order_id="fake_sell_1")


class _ScriptedStrategy:
    """Emits scripted entry / exit signals."""

    name = "TestStrategy"

    def __init__(
        self,
        entry_seq: list[SignalType],
        exit_seq: list[SignalType] | None = None,
        stop_loss: float = 50.0,
        take_profit: float = 200.0,
    ) -> None:
        self._entry_seq = entry_seq
        self._exit_seq = list(exit_seq or [])
        self._ei = 0
        self._xi = 0
        self._sl = stop_loss
        self._tp = take_profit

    def initialize(self) -> None:
        pass

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        idx = min(self._ei, len(self._entry_seq) - 1)
        sig = self._entry_seq[idx]
        self._ei += 1
        price = float(df["close"].iloc[-1]) if not df.empty else 100.0
        return StrategySignal(
            signal=sig, price=price,
            timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
            score=1.0 if sig == SignalType.BUY else 0.0,
            stop_loss=self._sl,
            take_profit=self._tp,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        sig = (
            self._exit_seq[min(self._xi, len(self._exit_seq) - 1)]
            if self._exit_seq else SignalType.HOLD
        )
        self._xi += 1
        price = float(df["close"].iloc[-1]) if not df.empty else entry_price
        return StrategySignal(
            signal=sig, price=price,
            timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
            score=0.0,
        )


def _noop_db() -> dict:
    """DB ops stub — safe without a real DB connection."""
    return {
        "create_trade": lambda **kw: 42,
        "update_after_buy": lambda **kw: None,
        "cancel_trade": lambda **kw: None,
        "close_trade": lambda **kw: None,
        "is_trade_open": lambda **kw: True,
        "find_open_trade": lambda **kw: None,
        "load_trade_state": lambda **kw: None,
        "load_all_open_trade_states": lambda: {},
    }


def _cfg(max_cycles: int) -> LiveTradingConfig:
    return LiveTradingConfig(
        symbol="BTC/USDT",
        timeframe="15m",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        poll_seconds=0.0,
        bootstrap_bars=200,
        bootstrap_replay_bars=100,
        max_cycles=max_cycles,
        resume=True,
        output_prefix="live",
    )


def _service(
    exchange: _ScriptedExchange,
    strategy: _ScriptedStrategy,
    fake_lrs: _FakeLRS,
    fake_oe: _FakeOE,
    store: _InMemoryPositionStore,
    db_ops: dict | None = None,
) -> LiveTradingService:
    return LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: strategy,
        sleep_fn=lambda _: None,
        position_store_factory=lambda _: store,
        live_risk_service_factory=lambda oe, rm, pv: fake_lrs,
        order_executor_factory=lambda ex: fake_oe,
        db_ops=db_ops if db_ops is not None else _noop_db(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reentry_blocked_while_position_open() -> None:
    """BUY on every candle: execute_market_buy_with_risk fires only once."""
    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),   # bootstrap
            _candle(110.0, 1),   # cycle 1 → BUY → open position
            _candle(115.0, 2),   # cycle 2 → position open → blocked
        ],
    )
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY, SignalType.BUY],
        stop_loss=50.0, take_profit=200.0,
    )

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store).run(_cfg(2))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 1, f"Expected 1 BUY, got {len(fake_lrs.calls)}"
    assert result["open_position"] is True


def test_full_buy_then_stop_loss_closes_position() -> None:
    """BUY on cycle 1; close falls below SL on cycle 2 → sell called once."""
    fake_lrs = _FakeLRS()
    fake_oe = _FakeOE()
    store = _InMemoryPositionStore()
    # entry ~115, SL=105; cycle 2 close=104 <= 105 triggers SL
    exchange = _ScriptedExchange(
        candles=[
            _candle(110.0, 0),   # bootstrap
            _candle(115.0, 1),   # cycle 1 → BUY
            _candle(104.0, 2),   # cycle 2 → SL hit
        ],
    )
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY, SignalType.HOLD],
        stop_loss=105.0, take_profit=300.0,
    )

    result = _service(exchange, strategy, fake_lrs, fake_oe, store).run(_cfg(2))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 1
    assert len(fake_oe.sell_calls) == 1
    assert fake_oe.sell_calls[0]["symbol"] == "BTC/USDT"
    assert store.load_all() == [], "State must be empty after close"
    assert result["open_position"] is False


def test_full_buy_then_take_profit_closes_position() -> None:
    """BUY on cycle 1; close rises above TP on cycle 2 → sell called once."""
    fake_lrs = _FakeLRS()
    fake_oe = _FakeOE()
    store = _InMemoryPositionStore()
    # entry ~115, TP=120; cycle 2 close=121 >= 120 triggers TP
    exchange = _ScriptedExchange(
        candles=[
            _candle(110.0, 0),
            _candle(115.0, 1),
            _candle(121.0, 2),
        ],
    )
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY, SignalType.HOLD],
        stop_loss=50.0, take_profit=120.0,
    )

    result = _service(exchange, strategy, fake_lrs, fake_oe, store).run(_cfg(2))

    assert len(fake_lrs.calls) == 1
    assert len(fake_oe.sell_calls) == 1
    assert result["open_position"] is False


def test_binance_order_rejection_continues_loop() -> None:
    """InsufficientFunds on BUY → process must not crash; trade must be cancelled."""
    cancelled: list[int] = []
    ops = _noop_db()
    ops["cancel_trade"] = lambda trade_id, **kw: cancelled.append(trade_id)

    fake_lrs = _FakeLRS(exc=ccxt.InsufficientFunds("not enough"))
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),
            _candle(110.0, 1),   # BUY → rejected
            _candle(111.0, 2),   # loop must continue without crash
        ],
    )
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY, SignalType.BUY],
        stop_loss=50.0, take_profit=200.0,
    )

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(2))

    assert result["status"] == "completed", "Loop must survive Binance rejection"
    assert result["open_position"] is False
    assert len(cancelled) >= 1, "Rejected trade must be cancelled in DB"


def test_restart_with_open_position_reconciles_from_state_file() -> None:
    """State file present + DB confirms OPEN → no new BUY, position resumed."""
    initial = LivePositionState(
        trade_id=99, symbol="BTC/USDT", quantity=0.001,
        timeframe="15m", strategy="ClassicDonchianBreakout",
        stake_amount=0.1, entry_price=100.0, stop_loss=50.0, take_profit=200.0,
        opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="ex_123",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    fake_lrs = _FakeLRS()

    ops = _noop_db()
    ops["is_trade_open"] = lambda trade_id, **kw: True
    ops["load_all_open_trade_states"] = lambda: {99: initial}

    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),   # bootstrap
            _candle(110.0, 1),   # cycle 1: between SL=50 and TP=200 → hold
        ],
    )
    # BUY signal but guard must block it (position already open)
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY],
        stop_loss=50.0, take_profit=200.0,
    )

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0, "No new BUY when position already open"
    assert result["open_position"] is True


def test_idempotency_skips_buy_when_open_orders_exist() -> None:
    """Binance already has open orders → BUY skipped (idempotency guard)."""
    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),
            _candle(110.0, 1),
        ],
        open_orders=[{"id": "existing_order", "symbol": "BTC/USDT"}],
    )
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY],
        stop_loss=50.0, take_profit=200.0,
    )

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0, "BUY must be skipped when Binance has open orders"
    assert result["open_position"] is False


def test_reconciliation_on_startup_no_open_position() -> None:
    """Clean startup: no state file, DB empty → open_position None, loop normal."""
    store = _InMemoryPositionStore()
    fake_lrs = _FakeLRS()

    ops = _noop_db()
    ops["find_open_trade"] = lambda **kw: None

    exchange = _ScriptedExchange(candles=[_candle(100.0, 0)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.HOLD])

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0
    assert result["open_position"] is False


def test_max_open_positions_blocks_new_buy() -> None:
    fake_lrs = _FakeLRS()
    states = [
        LivePositionState(
            trade_id=1, symbol="BTC/USDT", timeframe="15m", strategy="ClassicDonchianBreakout",
            quantity=0.001, stake_amount=10.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
            opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="",
        ),
        LivePositionState(
            trade_id=2, symbol="ETH/USDT", timeframe="15m", strategy="ClassicDonchianBreakout",
            quantity=0.001, stake_amount=10.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
            opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="",
        ),
        LivePositionState(
            trade_id=3, symbol="SOL/USDT", timeframe="15m", strategy="ClassicDonchianBreakout",
            quantity=0.001, stake_amount=10.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
            opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="",
        ),
    ]
    store = _InMemoryPositionStore(initial_states=states)
    exchange = _ScriptedExchange(candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {state.trade_id: state for state in states}

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0


def test_context_uniqueness_blocks_duplicate_buy() -> None:
    fake_lrs = _FakeLRS()
    initial = LivePositionState(
        trade_id=1, symbol="BTC/USDT", timeframe="15m", strategy="ClassicDonchianBreakout",
        quantity=0.001, stake_amount=10.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    exchange = _ScriptedExchange(candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {initial.trade_id: initial}

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0


def test_min_free_usdt_reserve_blocks_buy() -> None:
    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(free_usdt=4.5, candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=109.0, take_profit=120.0)

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0


def test_multi_asset_same_cycle_sell_btc_and_buy_eth_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OrderedSet:
        def __init__(self, values=()) -> None:
            self._items: list[tuple[str, str, str]] = []
            for value in values:
                if value not in self._items:
                    self._items.append(value)

        def __iter__(self):
            return iter(self._items)

        def __contains__(self, item: object) -> bool:
            return item in self._items

        def __len__(self) -> int:
            return len(self._items)

        def __or__(self, other):
            merged = _OrderedSet(self._items)
            for value in other:
                if value not in merged._items:
                    merged._items.append(value)
            return merged

    monkeypatch.setitem(live_service_module.__dict__, "set", _OrderedSet)

    class _PriceRuleStrategy:
        name = "TestStrategy"

        def initialize(self) -> None:
            return None

        def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
            price = float(df["close"].iloc[-1])
            if price >= 200.0:
                return StrategySignal(
                    signal=SignalType.BUY,
                    price=price,
                    timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
                    score=1.0,
                    stop_loss=180.0,
                    take_profit=240.0,
                )
            return StrategySignal(
                signal=SignalType.HOLD,
                price=price,
                timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
                score=0.0,
            )

        def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
            price = float(df["close"].iloc[-1])
            return StrategySignal(
                signal=SignalType.HOLD,
                price=price,
                timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
                score=0.0,
            )

    btc_open = LivePositionState(
        trade_id=101,
        symbol="BTC/USDT",
        timeframe="15m",
        strategy="ClassicDonchianBreakout",
        quantity=0.001,
        stake_amount=8.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=140.0,
        opened_at="2026-07-09T00:00:00+00:00",
        exchange_order_id="btc_open_101",
    )

    store = _InMemoryPositionStore(initial_states=[btc_open])
    fake_lrs = _FakeLRS()
    fake_oe = _FakeOE()

    exchange = _ScriptedExchange(
        free_usdt=10.0,
        candles_by_symbol={
            "BTC/USDT": [_candle(100.0, 0), _candle(90.0, 1)],
            "ETH/USDT": [_candle(205.0, 0), _candle(210.0, 1)],
        },
    )

    created: list[dict] = []
    closed: list[int] = []

    ops = _noop_db()
    ops["create_trade"] = lambda **kw: (created.append(kw) or 202)
    ops["close_trade"] = lambda trade_id, **kw: closed.append(trade_id)
    ops["load_all_open_trade_states"] = lambda: {btc_open.trade_id: btc_open}

    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: _PriceRuleStrategy(),
        sleep_fn=lambda _: None,
        position_store_factory=lambda _: store,
        live_risk_service_factory=lambda oe, rm, pv: fake_lrs,
        order_executor_factory=lambda ex: fake_oe,
        db_ops=ops,
    )

    cfg = LiveTradingConfig(
        symbol="BTC/USDT",
        symbols=("BTC/USDT", "ETH/USDT"),
        timeframe="15m",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        poll_seconds=0.0,
        bootstrap_bars=200,
        bootstrap_replay_bars=100,
        max_cycles=1,
        resume=True,
        output_prefix="live",
    )

    result = service.run(cfg)

    assert result["status"] == "completed"
    assert result["cycles"] == 1

    assert len(fake_oe.sell_calls) == 1
    assert fake_oe.sell_calls[0]["symbol"] == "BTC/USDT"
    assert closed == [101]

    assert len(fake_lrs.calls) == 1
    assert fake_lrs.calls[0]["symbol"] == "ETH/USDT"

    persisted = store.load_all()
    assert len(persisted) == 1
    assert persisted[0].symbol == "ETH/USDT"
    assert persisted[0].strategy == "ClassicDonchianBreakout"
    assert persisted[0].timeframe == "15m"

    assert len({state.context_key for state in persisted}) == 1
    assert persisted[0].context_key == ("ETH/USDT", "ClassicDonchianBreakout", "15m")

    assert created, "BUY de ETH deve persistir nova trade"
    assert float(created[0]["stake_amount"]) == 0.0
    assert result["open_position"] is True
    assert result["open_positions"] == 1


def test_startup_recovery_fails_safe_when_db_unavailable() -> None:
    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(candles=[_candle(100.0, 0)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.HOLD])

    ops = _noop_db()

    def _raise_db() -> dict[int, LivePositionState]:
        raise RuntimeError("db_down")

    ops["load_all_open_trade_states"] = _raise_db

    with pytest.raises(RuntimeError, match="Banco indisponivel durante startup/recovery LIVE"):
        _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))


def test_live_position_store_recovers_from_backup_when_primary_is_corrupted(tmp_path: Path) -> None:
    state_file = tmp_path / "live_positions.json"
    store = live_service_module._LivePositionStore(state_file)

    expected = LivePositionState(
        trade_id=17,
        symbol="BTC/USDT",
        timeframe="15m",
        strategy="ClassicDonchianBreakout",
        quantity=0.001,
        stake_amount=0.1,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        opened_at="2026-07-09T00:00:00+00:00",
        exchange_order_id="ord-17",
    )

    store.save_all([expected])
    state_file.write_text("{invalid-json", encoding="utf-8")

    restored = store.load_all()
    assert len(restored) == 1
    assert restored[0].trade_id == 17
    assert restored[0].symbol == "BTC/USDT"


def test_live_bound_frame_caps_memory_after_thousands_of_cycles() -> None:
    start = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        [
            {
                "open": 99.9,
                "high": 100.1,
                "low": 99.8,
                "close": 100.0,
                "volume": 10.0,
            }
            for _ in range(50)
        ],
        index=pd.DatetimeIndex([start + pd.Timedelta(minutes=15 * i) for i in range(50)]),
    )

    for i in range(5000):
        ts = start + pd.Timedelta(minutes=15 * (50 + i))
        latest = pd.DataFrame(
            [{"open": 100.0, "high": 101.0, "low": 99.5, "close": 101.0 + (i * 0.001), "volume": 10.0}],
            index=pd.DatetimeIndex([ts]),
        )
        frame = pd.concat([frame, latest]).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = LiveTradingService._bound_frame(frame, 300)

    assert len(frame) <= 300
