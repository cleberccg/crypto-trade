"""
Testes do ciclo completo de execução ao vivo implementado em LiveTradingService.

Cobertura:
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
from contextlib import contextmanager
from dataclasses import replace
import json

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
# Funções auxiliares
# ---------------------------------------------------------------------------

class _InMemoryPositionStore:
    """Substituto em memória para _LivePositionStore."""

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
    """Namespace genérico de atributos para simular objetos ORM/dataclass."""
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
    """Exchange simulada que fornece uma sequência predefinida de candles."""

    def __init__(
        self,
        free_usdt: float = 100.0,
        candles: list[pd.DataFrame] | None = None,
        candles_by_symbol: dict[str, list[pd.DataFrame]] | None = None,
        open_orders: list[dict] | None = None,
        asset_balances: dict[str, float] | None = None,
    ) -> None:
        self.free_usdt = free_usdt
        self.asset_balances = dict(asset_balances or {})
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
        free = {"USDT": self.free_usdt, **self.asset_balances}
        return {"free": free}

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
    """LiveRiskService simulada — registra chamadas e pode lançar exceções."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._exc = exc

    def execute_market_buy_with_risk(self, trade, symbol, entry_price,
                                     stop_loss, take_profit, **kw):
        if self._exc is not None:
            raise self._exc
        self.calls.append({"symbol": symbol, "entry_price": entry_price})
        order = _Obj(price=entry_price, filled_quantity=0.001,
                     exchange_order_id="fake_buy_1", fee_usdt=0.0,
                     fee_source="REPORTED", base_fee_quantity=0.0)
        risk_params = _Obj(quantity=0.001, stake_amount=0.1)
        return _Obj(order=order, risk_params=risk_params, portfolio_value=100.0)


class _FakeOE:
    """OrderExecutor simulado — registra chamadas de venda e pode lançar exceções."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.sell_calls: list[dict] = []
        self._exc = exc

    def execute_market_sell(self, trade, symbol, quantity, price):
        if self._exc is not None:
            raise self._exc
        self.sell_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        return _Obj(price=price, filled_quantity=quantity,
                    exchange_order_id="fake_sell_1", fee_usdt=0.0,
                    fee_source="REPORTED", base_fee_quantity=0.0)


class _ScriptedStrategy:
    """Emite sinais de entrada/saída predefinidos."""

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
    """Simulação das operações de banco de dados — segura sem conexão real."""
    return {
        "create_trade": lambda **kw: 42,
        "update_after_buy": lambda **kw: None,
        "cancel_trade": lambda **kw: None,
        "close_trade": lambda **kw: None,
        "is_trade_open": lambda **kw: True,
        "find_open_trade": lambda **kw: None,
        "load_trade_state": lambda **kw: None,
        "load_all_open_trade_states": lambda: {},
        "record_live_exit": lambda **kw: None,
        "mark_dust": lambda **kw: None,
    }


def _cfg(max_cycles: int, symbol: str = "BTC/USDT", timeframe: str = "15m") -> LiveTradingConfig:
    return LiveTradingConfig(
        symbol=symbol,
        timeframe=timeframe,
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
    service = LiveTradingService(
        base_dir=Path("."),
        exchange_factory=lambda: exchange,
        strategy_factory=lambda _: strategy,
        sleep_fn=lambda _: None,
        position_store_factory=lambda _: store,
        live_risk_service_factory=lambda oe, rm, pv: fake_lrs,
        order_executor_factory=lambda ex: fake_oe,
        db_ops=db_ops if db_ops is not None else _noop_db(),
    )
    # Fixtures antigas usam julho/2026; atualidade e testada separadamente abaixo.
    service._is_current_closed_candle = lambda frame, timeframe: True
    return service


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------

def test_reentry_blocked_while_position_open() -> None:
    """Mesmo com BUY em todos os candles, execute_market_buy_with_risk é chamado apenas uma vez."""
    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),   # bootstrap
            _candle(110.0, 1),   # ciclo 1 → BUY → abre posição
            _candle(115.0, 2),   # ciclo 2 → posição aberta → bloqueado
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
    """BUY no ciclo 1; o fechamento cai abaixo de SL no ciclo 2 → sell é chamado uma vez."""
    fake_lrs = _FakeLRS()
    fake_oe = _FakeOE()
    store = _InMemoryPositionStore()
    # Entrada ~115, SL=105; o fechamento do ciclo 2 em 104 <= 105 aciona o SL
    exchange = _ScriptedExchange(
        candles=[
            _candle(110.0, 0),   # bootstrap
            _candle(115.0, 1),   # ciclo 1 → BUY
            _candle(104.0, 2),   # ciclo 2 → SL atingido
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
    """BUY no ciclo 1; o fechamento sobe acima de TP no ciclo 2 → sell é chamado uma vez."""
    fake_lrs = _FakeLRS()
    fake_oe = _FakeOE()
    store = _InMemoryPositionStore()
    # Entrada ~115, TP=120; o fechamento do ciclo 2 em 121 >= 120 aciona o TP
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
    """InsufficientFunds em BUY → o processo não deve falhar e a operação deve ser cancelada."""
    cancelled: list[int] = []
    ops = _noop_db()
    ops["cancel_trade"] = lambda trade_id, **kw: cancelled.append(trade_id)

    fake_lrs = _FakeLRS(exc=ccxt.InsufficientFunds("not enough"))
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        candles=[
            _candle(100.0, 0),
            _candle(110.0, 1),   # BUY → rejeitado
            _candle(111.0, 2),   # O loop deve continuar sem falhar
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
    """Com arquivo de estado e DB confirmando OPEN → não há novo BUY e a posição é retomada."""
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
        asset_balances={"BTC": initial.quantity},
        candles=[
            _candle(100.0, 0),   # bootstrap
            _candle(110.0, 1),   # ciclo 1: entre SL=50 e TP=200 → mantém a posição
        ],
    )
    # Há sinal BUY, mas a proteção deve bloqueá-lo (já existe uma posição aberta)
    strategy = _ScriptedStrategy(
        entry_seq=[SignalType.BUY],
        stop_loss=50.0, take_profit=200.0,
    )

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0, "No new BUY when position already open"
    assert result["open_position"] is True


def test_restart_state_exchange_mismatch_blocks_startup() -> None:
    initial = LivePositionState(
        trade_id=99, symbol="BTC/USDT", quantity=0.001,
        timeframe="15m", strategy="ClassicDonchianBreakout",
        stake_amount=0.1, entry_price=100.0, stop_loss=50.0, take_profit=200.0,
        opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="ex_123",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {99: initial}
    exchange = _ScriptedExchange(asset_balances={"BTC": 0.0005}, candles=[_candle(100.0, 0)])

    with pytest.raises(RuntimeError, match="not covered by the Binance balance"):
        _service(exchange, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), store, ops).run(_cfg(1))


def test_unknown_bnb_blocks_buy_without_claiming_ownership() -> None:
    fake_lrs = _FakeLRS()
    exchange = _ScriptedExchange(
        asset_balances={"BNB": 0.1},
        candles=[_candle(100.0, 0), _candle(110.0, 1)],
    )
    strategy = _ScriptedStrategy([SignalType.BUY])

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), _InMemoryPositionStore()).run(
        _cfg(1, symbol="BNB/USDT", timeframe="4h")
    )

    assert result["open_position"] is False
    assert fake_lrs.calls == []


def test_emergency_exit_dry_run_uses_only_persisted_quantity() -> None:
    position = LivePositionState(
        trade_id=44, symbol="BNB/USDT", quantity=0.01,
        timeframe="4h", strategy="Sma200RegimeGated",
        stake_amount=7.7, entry_price=770.0, stop_loss=700.0, take_profit=900.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-44",
    )
    exchange = _ScriptedExchange(asset_balances={"BNB": 0.015})
    fake_oe = _FakeOE()
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {position.trade_id: position}
    service = _service(exchange, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), fake_oe, _InMemoryPositionStore(position), ops)

    result = service.emergency_exit(strategy_name="Sma200RegimeGated", symbol="BNB/USDT")

    assert result["bot_position_quantity"] == pytest.approx(0.01)
    assert result["estimated_notional"] == pytest.approx(1.0)
    assert result["would_sell"] is True
    assert result["real_order_sent"] is False
    assert fake_oe.sell_calls == []


def test_partial_sell_keeps_only_remaining_bot_quantity() -> None:
    position = LivePositionState(
        trade_id=45, symbol="BNB/USDT", quantity=0.01,
        timeframe="4h", strategy="Sma200RegimeGated",
        stake_amount=7.7, entry_price=770.0, stop_loss=700.0, take_profit=900.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-45",
        original_quantity=0.01, entry_fee_usdt=0.0077,
        fee_accounting_complete=True, realized_net_pnl=0.0,
    )
    updated: list[dict] = []
    ops = _noop_db()
    ops["record_live_exit"] = lambda **kw: updated.append(kw)
    service = _service(_ScriptedExchange(), _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), _InMemoryPositionStore(), ops)

    class _PartialSell:
        def execute_market_sell(self, trade, symbol, quantity, price):
            return _Obj(price=price, filled_quantity=quantity / 2, fee_usdt=0.001,
                        fee_source="REPORTED", base_fee_quantity=0.0)

    closed = service._try_close_position(position, 780.0, "emergency_exit", _PartialSell())

    assert closed is False
    assert position.quantity == pytest.approx(0.005)
    assert updated[0]["trade_id"] == 45
    assert updated[0]["remaining_quantity"] == pytest.approx(0.005)
    assert updated[0]["gross_pnl"] == pytest.approx(0.05)
    assert updated[0]["total_fees_usdt"] == pytest.approx(0.0087)
    assert updated[0]["pnl"] == pytest.approx(0.04515)


def test_partial_sell_restart_then_final_sell_preserves_accounting(tmp_path: Path) -> None:
    position = LivePositionState(
        trade_id=46, symbol="BNB/USDT", quantity=0.01,
        timeframe="4h", strategy="Sma200RegimeGated",
        stake_amount=1.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-46",
        original_quantity=0.01, entry_fee_usdt=0.01,
        fee_accounting_complete=True, realized_net_pnl=0.0,
    )
    persisted: dict[str, object] = {}

    def save_accounting(**values) -> None:
        persisted.update(values)

    state_path = tmp_path / "live_positions.json"
    store = live_service_module._LivePositionStore(state_path)
    service = _service(
        _ScriptedExchange(asset_balances={"BNB": 0.01}),
        _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), store,
        {**_noop_db(), "record_live_exit": save_accounting},
    )

    class _FirstPartial:
        def execute_market_sell(self, trade, symbol, quantity, price):
            return _Obj(price=110.0, filled_quantity=0.004, fee_usdt=0.004,
                        fee_source="REPORTED", base_fee_quantity=0.0)

    assert service._try_close_position(position, 110.0, "strategy_exit", _FirstPartial()) is False
    store.save_all([position])
    assert position.quantity == pytest.approx(0.006)

    db_position = LivePositionState(
        trade_id=46, symbol="BNB/USDT", quantity=float(persisted["remaining_quantity"]),
        timeframe="4h", strategy="Sma200RegimeGated", stake_amount=1.0,
        entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-46",
        original_quantity=0.01, entry_fee_usdt=0.01,
        entry_fee_allocated_usdt=float(persisted["entry_fee_allocated_usdt"]),
        realized_gross_pnl=float(persisted["gross_pnl"]),
        realized_exit_fees_usdt=float(persisted["exit_fee_usdt"]),
        realized_net_pnl=float(persisted["pnl"]),
        fee_accounting_complete=bool(persisted["fee_accounting_complete"]),
    )
    restarted_store = live_service_module._LivePositionStore(state_path)
    restarted_state = restarted_store.load_all()[0]
    assert restarted_state.original_quantity == pytest.approx(0.01)
    assert restarted_state.quantity == pytest.approx(0.006)
    assert restarted_state.entry_fee_usdt == pytest.approx(0.01)
    assert restarted_state.entry_fee_allocated_usdt == pytest.approx(0.004)
    assert restarted_state.realized_gross_pnl == pytest.approx(0.04)
    assert restarted_state.realized_exit_fees_usdt == pytest.approx(0.004)
    assert restarted_state.realized_net_pnl == pytest.approx(0.032)

    exchange = _ScriptedExchange(asset_balances={"BNB": 0.006})
    recovery_ops = {
        **_noop_db(),
        "load_all_open_trade_states": lambda: {46: db_position},
        "record_live_exit": save_accounting,
    }
    restarted_service = _service(exchange, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), restarted_store, recovery_ops)
    restored = restarted_service._reconcile_on_startup(
        cfg=LiveTradingConfig(
            symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated",
            strategy_version="v1", poll_seconds=0.0, max_cycles=1,
        ),
        exchange=exchange,
        store=restarted_store,
        target_contexts={("BNB/USDT", "Sma200RegimeGated", "4h")},
    )
    recovered = restored[("BNB/USDT", "Sma200RegimeGated", "4h")]
    assert recovered.entry_fee_allocated_usdt == pytest.approx(0.004)
    assert recovered.realized_gross_pnl == pytest.approx(0.04)

    class _FinalFill:
        def execute_market_sell(self, trade, symbol, quantity, price):
            return _Obj(price=120.0, filled_quantity=0.006, fee_usdt=0.006,
                        fee_source="REPORTED", base_fee_quantity=0.0)

    assert restarted_service._try_close_position(recovered, 120.0, "strategy_exit", _FinalFill()) is True
    assert persisted["gross_pnl"] == pytest.approx(0.16)
    assert persisted["total_fees_usdt"] == pytest.approx(0.02)
    assert persisted["pnl"] == pytest.approx(0.14)
    assert persisted["fully_closed"] is True


def test_live_closed_candles_are_utc_closed_and_deduplicated() -> None:
    now = pd.Timestamp.now(tz="UTC").floor("h")
    closed_time = now - pd.Timedelta(hours=8)
    open_time = now - pd.Timedelta(hours=2)
    frame = pd.DataFrame(
        [
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.0, "volume": 1.0},
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0},
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 2.0, "volume": 1.0},
        ],
        index=pd.DatetimeIndex([closed_time, closed_time, open_time]),
    )

    result = LiveTradingService._closed_only(frame, "4h")

    assert result is not None
    assert len(result) == 1
    assert result.index[0] == closed_time
    assert result.index.tz is not None
    assert float(result["close"].iloc[0]) == 1.5


def test_idempotency_skips_buy_when_open_orders_exist() -> None:
    """Se a Binance já tem ordens abertas, BUY é ignorado (proteção de idempotência)."""
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
    """Inicialização limpa: sem arquivo de estado e DB vazio → open_position None, ciclo normal."""
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
    exchange = _ScriptedExchange(asset_balances={"BTC": 0.001}, candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {state.trade_id: state for state in states}

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0


# ---------------------------------------------------------------------------
# Tolerância a BNB residual (poeira) — a verificação de titularidade deve aceitar saldos
# não negociáveis sem jamais tratá-los como pertencentes ao bot.
# ---------------------------------------------------------------------------

def test_caso1_small_residual_tolerated_allows_buy() -> None:
    """Um saldo residual de poeira (notional << min_notional) NÃO deve bloquear um BUY."""
    fake_lrs = _FakeLRS()
    # 0.00030331 BNB * 100.0 (preço fictício do ticker) = 0.03 USDT, muito abaixo do
    # valor alternativo padrão de min_notional (5.0), usado quando a exchange não fornece
    # fetch_symbol_trading_filters.
    exchange = _ScriptedExchange(
        asset_balances={"BNB": 0.00030331},
        candles=[_candle(100.0, 0), _candle(110.0, 1)],
    )
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), _InMemoryPositionStore()).run(
        _cfg(1, symbol="BNB/USDT", timeframe="4h")
    )

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 1, "Tolerated dust residual must not block BUY"
    assert result["open_position"] is True


def test_caso2_material_unknown_balance_blocks_buy_fail_closed() -> None:
    """Um saldo desconhecido material/negociável deve bloquear BUY (falha em modo seguro)."""
    # Mesmo fixture de test_unknown_bnb_blocks_buy_without_claiming_ownership:
    # 0.1 BNB * 100.0 = 10 USDT, acima do valor alternativo min_notional de 5.0.
    fake_lrs = _FakeLRS()
    exchange = _ScriptedExchange(
        asset_balances={"BNB": 0.1},
        candles=[_candle(100.0, 0), _candle(110.0, 1)],
    )
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY])

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), _InMemoryPositionStore()).run(
        _cfg(1, symbol="BNB/USDT", timeframe="4h")
    )

    assert result["open_position"] is False
    assert fake_lrs.calls == []


def test_caso3_residual_and_bot_buy_ownership_separated() -> None:
    """Após um BUY com saldo residual de poeira preexistente, BOT_OWNED deve
    corresponder somente à quantidade executada, nunca a residual + execução."""
    fake_lrs = _FakeLRS()  # Preenche uma quantidade fixa de 0.001
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(
        asset_balances={"BNB": 0.00030331},
        candles=[_candle(100.0, 0), _candle(110.0, 1)],
    )
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store).run(
        _cfg(1, symbol="BNB/USDT", timeframe="4h")
    )

    assert result["open_position"] is True
    assert len(store.saved) == 1
    bot_owned = float(store.saved[0].quantity)
    assert bot_owned == pytest.approx(0.001), "Bot-owned quantity must equal only the fill, not residual+fill"


def test_caso4_sell_sends_only_bot_quantity_with_residual_present() -> None:
    """SELL deve solicitar exatamente position.quantity pertencente ao bot,
    mesmo que a carteira também contenha um saldo residual não relacionado."""
    position = LivePositionState(
        trade_id=44, symbol="BNB/USDT", quantity=0.01,
        timeframe="4h", strategy="Sma200RegimeGated",
        stake_amount=7.7, entry_price=770.0, stop_loss=700.0, take_profit=900.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-44",
    )
    fake_oe = _FakeOE()
    # A carteira contém a quantidade do bot (0.01) MAIS um saldo residual não relacionado (0.00030331).
    service = _service(
        _ScriptedExchange(asset_balances={"BNB": 0.01030331}),
        _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), fake_oe, _InMemoryPositionStore(), _noop_db(),
    )

    closed = service._try_close_position(position, 780.0, "regime_not_bull", fake_oe)

    assert closed is True
    assert len(fake_oe.sell_calls) == 1
    assert fake_oe.sell_calls[0]["quantity"] == pytest.approx(0.01), "Must never sell TOTAL_WALLET_BNB"


def test_caso5_restart_preserves_bot_position_and_residual_separately() -> None:
    """A reconciliação após reinicialização deve restaurar exatamente a
    quantidade do bot registrada no DB/estado, mesmo que a carteira também
    contenha um saldo residual não rastreado."""
    initial = LivePositionState(
        trade_id=46, symbol="BNB/USDT", quantity=0.01,
        timeframe="4h", strategy="Sma200RegimeGated",
        stake_amount=7.7, entry_price=770.0, stop_loss=700.0, take_profit=900.0,
        opened_at="2026-09-29T00:00:00+00:00", exchange_order_id="bnb-buy-46",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {46: initial}
    # Carteira = quantidade do bot (0.01) + saldo residual não rastreado (0.00030331), ativos fungíveis.
    exchange = _ScriptedExchange(asset_balances={"BNB": 0.01030331})
    service = _service(exchange, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), store, ops)

    restored = service._reconcile_on_startup(
        cfg=LiveTradingConfig(
            symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated",
            strategy_version="v1", poll_seconds=0.0, max_cycles=1,
        ),
        exchange=exchange,
        store=store,
        target_contexts={("BNB/USDT", "Sma200RegimeGated", "4h")},
    )

    recovered = restored[("BNB/USDT", "Sma200RegimeGated", "4h")]
    assert recovered.quantity == pytest.approx(0.01), "Residual must not be folded into bot ownership"


def test_caso6_material_unexplained_wallet_state_diff_fails_closed() -> None:
    """Uma diferença MATERIAL sem explicação entre DB/estado e saldo da carteira
    deve falhar em modo seguro (mesma fixture de
    test_restart_state_exchange_mismatch_blocks_startup)."""
    initial = LivePositionState(
        trade_id=99, symbol="BTC/USDT", quantity=0.001,
        timeframe="15m", strategy="ClassicDonchianBreakout",
        stake_amount=0.1, entry_price=100.0, stop_loss=50.0, take_profit=200.0,
        opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="ex_123",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {99: initial}
    # A carteira tem apenas metade da quantidade esperada pelo banco/estado -> diferença material e não explicada.
    exchange = _ScriptedExchange(asset_balances={"BTC": 0.0005}, candles=[_candle(100.0, 0)])

    with pytest.raises(RuntimeError, match="not covered by the Binance balance"):
        _service(exchange, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), _FakeOE(), store, ops).run(_cfg(1))


def test_context_uniqueness_blocks_duplicate_buy() -> None:
    fake_lrs = _FakeLRS()
    initial = LivePositionState(
        trade_id=1, symbol="BTC/USDT", timeframe="15m", strategy="ClassicDonchianBreakout",
        quantity=0.001, stake_amount=10.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        opened_at="2026-07-09T00:00:00+00:00", exchange_order_id="",
    )
    store = _InMemoryPositionStore(initial_state=initial)
    exchange = _ScriptedExchange(asset_balances={"BTC": 0.001}, candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=90.0, take_profit=150.0)
    ops = _noop_db()
    ops["load_all_open_trade_states"] = lambda: {initial.trade_id: initial}

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store, ops).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == 0


@pytest.mark.parametrize(
    "free_usdt, stake_pct, expected_stake, expected_buy_calls",
    [
        pytest.param(4.5, 0.02, 0.09, 1, id="reserve-preserved"),
        pytest.param(4.08, 0.02, 0.0816, 0, id="reserve-violated"),
        pytest.param(5.0, 0.20, 1.0, 1, id="reserve-exactly-met"),
    ],
)
def test_min_free_usdt_reserve_blocks_buy(
    monkeypatch: pytest.MonkeyPatch,
    free_usdt: float,
    stake_pct: float,
    expected_stake: float,
    expected_buy_calls: int,
) -> None:
    # Fix only this test's settings; do not inherit financial assumptions from .env.
    monkeypatch.setattr(
        live_service_module.settings, "trading",
        replace(live_service_module.settings.trading,
                stake_amount_pct=stake_pct, min_free_usdt_reserve=4.0),
    )
    preview = live_service_module.RiskManager().evaluate_trade(
        portfolio_value=free_usdt, entry_price=110.0,
        stop_loss=109.0, take_profit=120.0, strategy_score=1.0,
    )
    assert preview.stake_amount == pytest.approx(expected_stake)
    assert ((free_usdt - preview.stake_amount) < 4.0) == (expected_buy_calls == 0)

    fake_lrs = _FakeLRS()
    store = _InMemoryPositionStore()
    exchange = _ScriptedExchange(free_usdt=free_usdt, candles=[_candle(100.0, 0), _candle(110.0, 1)])
    strategy = _ScriptedStrategy(entry_seq=[SignalType.BUY], stop_loss=109.0, take_profit=120.0)

    result = _service(exchange, strategy, fake_lrs, _FakeOE(), store).run(_cfg(1))

    assert result["status"] == "completed"
    assert len(fake_lrs.calls) == expected_buy_calls
    assert result["open_position"] is bool(expected_buy_calls)


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

        def discard(self, value) -> None:
            if value in self._items:
                self._items.remove(value)

        def __or__(self, other):
            merged = _OrderedSet(self._items)
            for value in other:
                if value not in merged._items:
                    merged._items.append(value)
            return merged

    monkeypatch.setitem(live_service_module.__dict__, "set", _OrderedSet)
    monkeypatch.setattr(LiveTradingService, "_is_current_closed_candle", staticmethod(lambda f, tf: True))

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
        asset_balances={"BTC": btc_open.quantity},
        candles_by_symbol={
            "BTC/USDT": [_candle(100.0, 0), _candle(90.0, 1)],
            "ETH/USDT": [_candle(210.0, 1), _candle(205.0, 0)],
        },
    )

    created: list[dict] = []
    closed: list[int] = []

    ops = _noop_db()
    ops["create_trade"] = lambda **kw: (created.append(kw) or 202)
    ops["record_live_exit"] = lambda trade_id, **kw: closed.append(trade_id)
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


# Regressao operacional: todos os saldos, ordens e IDs abaixo sao fixtures.
class _DustExchange(_ScriptedExchange):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.filters = {"min_qty": 0.001, "step_size": 0.001, "min_notional": 5.0}
        self.fee_rate = 0.001

    def fetch_symbol_trading_filters(self, symbol):
        return dict(self.filters)

    def fetch_trading_fee(self, symbol):
        return {"taker": self.fee_rate}

    def fetch_ticker(self, symbol):
        return {"last": 721.14, "high": 721.14}


class _FilteredSell(_FakeOE):
    def __init__(self, exchange, fee=0.0, exc=None):
        super().__init__(exc)
        self.exchange = exchange
        self.base_fee = fee

    def execute_market_sell(self, trade, symbol, quantity, price):
        self.sell_calls.append({"symbol": symbol, "quantity": quantity})
        if self._exc:
            raise self._exc
        self.exchange.asset_balances[symbol.split("/")[0]] -= quantity + self.base_fee
        return _Obj(price=price, filled_quantity=quantity, fee_usdt=self.base_fee * price,
                    fee_source="REPORTED", base_fee_quantity=self.base_fee)


def _audit_position(**kwargs):
    values = dict(trade_id=3081, symbol="BNB/USDT", timeframe="4h", strategy="Sma200RegimeGated",
                  quantity=0.00098726, stake_amount=7.08192, entry_price=787.152,
                  stop_loss=7.8688, take_profit=78688.0, opened_at="2026-10-04T00:00:11+00:00",
                  exchange_order_id="fixture-buy", original_quantity=0.00899325,
                  entry_fee_usdt=0.00531328, entry_fee_allocated_usdt=0.00473,
                  realized_gross_pnl=-0.448098, realized_exit_fees_usdt=0.00437953,
                  realized_net_pnl=-0.457207, fee_accounting_complete=True)
    values.update(kwargs)
    return LivePositionState(**values)


def _current_frame(offset=0, price=721.14):
    frame = _candle(price)
    closed = pd.Timestamp.now(tz="UTC").floor("4h") - pd.Timedelta(hours=4)
    frame.index = pd.DatetimeIndex([closed + pd.Timedelta(hours=4 * offset)])
    return frame


def _dust_lab(position=None, exchange=None, store=None, **ops):
    p = position or _audit_position()
    ex = exchange or _DustExchange(asset_balances={"BNB": p.quantity + 0.00030331, "ETH": 2.0})
    oe = _FilteredSell(ex)
    db = {**_noop_db(), **ops}
    service = _service(ex, _ScriptedStrategy([SignalType.HOLD]), _FakeLRS(), oe,
                       store or _InMemoryPositionStore(), db)
    return service, ex, oe, p


def test_buy_bnb_fee_keeps_only_owned_net_quantity():
    service, ex, _, _ = _dust_lab()
    class Buy(_FakeLRS):
        def execute_market_buy_with_risk(self, **kwargs):
            return _Obj(order=_Obj(price=787.152, filled_quantity=0.009,
                        exchange_order_id="fixture", base_fee_quantity=0.00000675,
                        fee_usdt=0.00531328, fee_source="REPORTED"),
                        risk_params=_Obj(stake_amount=7.08192))
    cfg = LiveTradingConfig(symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated", strategy_version="v1")
    signal = StrategySignal(SignalType.BUY, 787.152, datetime.now(timezone.utc),
                            score=1, stop_loss=780, take_profit=810)
    position = service._try_open_position(cfg, signal, Buy(), live_service_module.RiskManager(), ex, 100)
    assert position.quantity == pytest.approx(0.00899325)
    assert position.original_quantity == pytest.approx(0.00899325)
    assert position.entry_fee_usdt == pytest.approx(0.00531328)


def test_sell_rounding_separates_dust_without_external_bnb_or_eth():
    p = _audit_position(quantity=0.00899325, entry_fee_allocated_usdt=0,
                        realized_gross_pnl=0, realized_exit_fees_usdt=0, realized_net_pnl=0)
    exits, classified = [], []
    s, ex, _, _ = _dust_lab(p, record_live_exit=lambda **kw: exits.append(kw),
                           mark_dust=lambda **kw: classified.append(kw))
    oe = _FilteredSell(ex, fee=0.00000599)
    assert not s._try_close_position(p, 731.14, "strategy_exit", oe)
    assert oe.sell_calls[0]["quantity"] == pytest.approx(0.008)
    assert p.quantity == pytest.approx(0.00098726)
    assert p.status == "DUST" and s._last_exit_filled
    assert ex.asset_balances["BNB"] - p.quantity == pytest.approx(0.00030331)
    assert ex.asset_balances["ETH"] == 2.0
    assert exits[0]["fully_closed"] is False and len(classified) == 1
    assert p.realized_net_pnl == pytest.approx(p.realized_gross_pnl - p.entry_fee_allocated_usdt - p.realized_exit_fees_usdt)


@pytest.mark.parametrize("quantity,filter_name", [(0.00098726, "LOT_SIZE"), (0.005, "NOTIONAL")])
def test_dust_filters_preserve_quantity_pnl_fees_and_origin(quantity, filter_name):
    s, ex, oe, p = _dust_lab(_audit_position(quantity=quantity))
    before = (p.quantity, p.realized_net_pnl, p.entry_fee_usdt, p.realized_exit_fees_usdt, p.opened_at)
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert filter_name in p.dust_reason and p.status == "DUST"
    assert before == (p.quantity, p.realized_net_pnl, p.entry_fee_usdt, p.realized_exit_fees_usdt, p.opened_at)
    assert not oe.sell_calls and not s._last_exit_filled


def test_known_dust_does_not_block_new_entry_or_merge_ownership():
    dust = _audit_position(status="DUST")
    ex = _DustExchange(asset_balances={"BNB": dust.quantity + 0.00030331},
                       candles=[_current_frame(-1), _current_frame()])
    store = _InMemoryPositionStore(initial_state=dust)
    s, _, _, _ = _dust_lab(dust, ex, store, load_all_open_trade_states=lambda: {dust.trade_id: dust})
    s._strategy_factory = lambda _: _ScriptedStrategy([SignalType.BUY], stop_loss=700, take_profit=800)
    buys = _FakeLRS()
    s._live_risk_service_factory = lambda *args: buys
    cfg = LiveTradingConfig(symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated",
                            strategy_version="v1", max_cycles=2)
    result = s.run(cfg)
    assert len(buys.calls) == 1 and result["open_positions"] == 1 and result["dust_positions"] == 1
    states = store.load_all()
    assert {p.trade_id for p in states} == {42, 3081}
    assert next(p for p in states if p.trade_id == 3081).quantity == dust.quantity
    assert result["capital_free"] == ex.free_usdt


def test_material_unknown_balance_still_blocks_with_known_dust():
    s, ex, _, p = _dust_lab()
    s._dust_positions[p.trade_id] = replace(p, status="DUST")
    ex.asset_balances["BNB"] = 0.1
    assert s._has_untracked_asset_balance(ex, "BNB/USDT")
    ex.asset_balances["BNB"] = p.quantity / 2
    assert s._has_untracked_asset_balance(ex, "BNB/USDT")


def test_restart_preserves_old_eth_and_multiple_dust_same_context(tmp_path):
    d1 = _audit_position(status="DUST")
    d2 = replace(d1, trade_id=3082)
    eth = replace(d1, trade_id=12, symbol="ETH/USDT", strategy="Legacy", quantity=2, status="OPEN")
    store = live_service_module._LivePositionStore(tmp_path / "positions.json")
    store.save_all([d1, d2, eth])
    ex = _DustExchange(asset_balances={"BNB": d1.quantity + d2.quantity + 0.00030331, "ETH": 2})
    s, _, oe, _ = _dust_lab(d1, ex, store, load_all_open_trade_states=lambda: {p.trade_id: p for p in (d1, d2, eth)})
    cfg = LiveTradingConfig(symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated", strategy_version="v1")
    restored = s._reconcile_on_startup(cfg, ex, store, {d1.context_key})
    assert not restored and len(s._dust_positions) == 2
    s._store_save_positions(store, restored)
    assert {p.trade_id for p in store.load_all()} == {12, 3081, 3082}
    assert not oe.sell_calls and ex.asset_balances["ETH"] == 2


def test_restart_checks_aggregate_ownership_not_each_trade_individually():
    d = _audit_position(status="DUST")
    active = replace(d, trade_id=4000, status="OPEN", quantity=0.01)
    ex = _DustExchange(asset_balances={"BNB": active.quantity})
    store = _InMemoryPositionStore(initial_states=[d, active])
    s, _, _, _ = _dust_lab(d, ex, store, load_all_open_trade_states=lambda: {p.trade_id:p for p in (d, active)})
    cfg = LiveTradingConfig(symbol=d.symbol, timeframe=d.timeframe, strategy_name=d.strategy, strategy_version="v1")
    with pytest.raises(RuntimeError, match="not covered"):
        s._reconcile_on_startup(cfg, ex, store, {d.context_key})


def test_capital_after_partial_is_remaining_cost_not_original_stake():
    s, ex, _, p = _dust_lab()
    assert s._capital_committed({p.context_key:p}) == pytest.approx(0.77712368352)
    p.status = "DUST"
    assert s._capital_committed({p.context_key:p}) == 0
    assert s._fetch_free_usdt(ex) == ex.free_usdt


def test_sell_blocked_logged_once_and_no_fill_invented(caplog):
    s, _, oe, p = _dust_lab(_audit_position(quantity=0.01))
    p.exit_pending = True
    for _ in range(3):
        assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    messages = [r.message for r in caplog.records if "SELL_BLOCKED" in r.message]
    assert len(messages) == 1 and "UNCERTAIN_PREVIOUS_SELL" in messages[0]
    assert not oe.sell_calls


def test_network_uncertainty_persists_intent_and_prevents_duplicate_after_restart(tmp_path):
    p = _audit_position(quantity=0.01)
    store = live_service_module._LivePositionStore(tmp_path / "positions.json")
    s, ex, _, _ = _dust_lab(p, store=store)
    s._runtime_store = store
    s._active_positions = {p.context_key:p}
    oe = _FilteredSell(ex, exc=ccxt.RequestTimeout("fixture timeout"))
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert store.load_all()[0].exit_pending
    s._store_save_positions(store, s._active_positions)
    restarted, _, _, _ = _dust_lab(p, ex, store, load_all_open_trade_states=lambda: {p.trade_id:replace(p, exit_pending=False)})
    cfg = LiveTradingConfig(symbol=p.symbol, timeframe=p.timeframe, strategy_name=p.strategy, strategy_version="v1")
    states = restarted._reconcile_on_startup(cfg, ex, store, {p.context_key})
    assert not restarted._try_close_position(states[p.context_key], 721.14, "strategy_exit", oe)
    assert len(oe.sell_calls) == 1


@pytest.mark.parametrize("stale,pending,processed", [(False,False,False),(True,False,False),
                                                   (False,True,False),(False,False,True)])
def test_recovery_evaluates_only_latest_closed_without_duplicate_or_stale_sell(stale, pending, processed):
    closed = _current_frame(-2 if stale else 0)
    p = _audit_position(quantity=0.01, exit_pending=pending,
                        last_evaluated_candle=closed.index[-1].isoformat() if processed else "")
    ex = _DustExchange(asset_balances={"BNB": p.quantity + 0.00030331},
                       candles=[pd.concat([closed, _current_frame(1, 999)])])
    store = _InMemoryPositionStore(initial_state=p)
    s, _, oe, _ = _dust_lab(p, ex, store, load_all_open_trade_states=lambda: {p.trade_id:p})
    # Usa a funcao de atualidade real, nao o adaptador das fixtures historicas.
    s._is_current_closed_candle = LiveTradingService._is_current_closed_candle
    strategy = _ScriptedStrategy([SignalType.BUY], exit_seq=[SignalType.SELL])
    seen = []
    def calculate(frame):
        seen.append(frame.index[-1])
        assert frame.index[-1] + pd.Timedelta(hours=4) <= pd.Timestamp.now(tz="UTC")
        return frame
    strategy.calculate = calculate
    s._strategy_factory = lambda _: strategy
    cfg = LiveTradingConfig(symbol=p.symbol, timeframe=p.timeframe, strategy_name=p.strategy,
                            strategy_version="v1", max_cycles=2)
    result = s.run(cfg)
    assert len(oe.sell_calls) == (0 if stale or pending or processed else 1)
    assert seen and seen[0] == closed.index[-1]
    if stale or pending:
        assert "SELL_BLOCKED" in result["last_signals"].values()


def test_stale_bootstrap_never_generates_buy():
    ex = _DustExchange(candles=[_current_frame(-3), _current_frame(-2)])
    s, _, _, _ = _dust_lab(exchange=ex)
    s._is_current_closed_candle = LiveTradingService._is_current_closed_candle
    buys = _FakeLRS()
    s._live_risk_service_factory = lambda *args: buys
    s._strategy_factory = lambda _: _ScriptedStrategy([SignalType.BUY])
    result = s.run(LiveTradingConfig(symbol="BNB/USDT", timeframe="4h", strategy_name="Sma200RegimeGated",
                                    strategy_version="v1", max_cycles=1))
    assert not buys.calls and not result["open_positions"]


def test_filter_failure_never_releases_ownership_or_sends_order():
    s, ex, oe, p = _dust_lab()
    ex.filters["step_size"] = 0
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert p.status == "OPEN" and not oe.sell_calls
    assert "FILTERS_UNAVAILABLE" in s._sell_blocks[p.trade_id]


def test_bnb_sell_reserves_own_fee_even_for_exact_lot():
    p = _audit_position(quantity=0.01)
    s, ex, _, _ = _dust_lab(p)
    ex.asset_balances["BNB"] = 0.01 + 0.00030331
    oe = _FilteredSell(ex, fee=0.00000675)
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert oe.sell_calls[0]["quantity"] == pytest.approx(0.009)
    assert ex.asset_balances["BNB"] - p.quantity == pytest.approx(0.00030331)


def test_dust_sql_transition_preserves_3081_accounting_and_reloads(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from database.models import Base, Trade
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    @contextmanager
    def isolated_session():
        with Session(engine) as session:
            yield session
            session.commit()
    monkeypatch.setattr(live_service_module, "get_session", isolated_session)
    p = _audit_position()
    with isolated_session() as session:
        session.add(Trade(id=3081, symbol=p.symbol, timeframe=p.timeframe, strategy_name=p.strategy,
                    side="BUY", status="OPEN", is_paper=False, entry_price=p.entry_price,
                    quantity=p.quantity, original_quantity=p.original_quantity, stake_amount=p.stake_amount,
                    entry_time=datetime(2026,10,4,tzinfo=timezone.utc), pnl=p.realized_net_pnl,
                    gross_pnl=p.realized_gross_pnl, entry_fee_usdt=p.entry_fee_usdt,
                    entry_fee_allocated_usdt=p.entry_fee_allocated_usdt, exit_fee_usdt=p.realized_exit_fees_usdt,
                    fee_accounting_complete=True))
        session.add(Trade(id=9, symbol="ETH/USDT", strategy_name="Paper", side="BUY", status="OPEN",
                    is_paper=True, entry_price=2000, quantity=1, stake_amount=2000,
                    entry_time=datetime(2026,1,1,tzinfo=timezone.utc)))
    s, _, oe, _ = _dust_lab(p)
    del s._db_ops["mark_dust"]
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    with isolated_session() as session:
        row = session.execute(text("SELECT status,quantity,pnl,entry_fee_usdt,exit_fee_usdt,exit_time,exit_price FROM trades WHERE id=3081")).one()
        assert tuple(row) == ("DUST", p.quantity, -0.457207, 0.00531328, 0.00437953, None, None)
    del s._db_ops["load_all_open_trade_states"]
    loaded = s._db_load_all_open_trade_states()
    assert list(loaded) == [3081] and loaded[3081].status == "DUST"
    # Nenhum fill pode ser aplicado novamente a um trade ja separado como dust.
    del s._db_ops["record_live_exit"]
    with pytest.raises(RuntimeError, match="accounting persistence failed"):
        s._db_record_live_exit(trade_id=3081, remaining_quantity=p.quantity,
                exit_price=731.14, gross_pnl=p.realized_gross_pnl, pnl=p.realized_net_pnl,
                pnl_pct=-6.45859, total_fees_usdt=0.00969281,
                entry_fee_allocated_usdt=p.entry_fee_allocated_usdt,
                exit_fee_usdt=p.realized_exit_fees_usdt, fee_accounting_complete=True,
                exit_reason="strategy_exit", exit_time=datetime.now(timezone.utc), fully_closed=False)
    engine.dispose()


def test_duplicate_open_context_fails_instead_of_discarding_owned_trade():
    p = _audit_position(quantity=0.01)
    other = replace(p, trade_id=4000)
    s, ex, _, _ = _dust_lab(p, store=_InMemoryPositionStore(initial_states=[p, other]))
    cfg = LiveTradingConfig(symbol=p.symbol, timeframe=p.timeframe, strategy_name=p.strategy, strategy_version="v1")
    with pytest.raises(RuntimeError, match="Duplicate persisted OPEN"):
        s._reconcile_on_startup(cfg, ex, _InMemoryPositionStore(initial_states=[p, other]), {p.context_key})


def test_dust_transition_persistence_failure_does_not_release_position():
    def fail(**kwargs):
        raise RuntimeError("fixture storage unavailable")
    s, _, oe, p = _dust_lab(mark_dust=fail)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert p.status == "OPEN" and not s._dust_positions and not oe.sell_calls


def test_corrupt_state_and_backup_fail_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("invalid")
    path.with_suffix(".json.bak").write_text("also invalid")
    with pytest.raises(RuntimeError, match="Corrupted LIVE"):
        live_service_module._LivePositionStore(path).load_all()


def test_dynamic_filters_not_hardcoded_to_bnb_step_or_five_usdt():
    s, ex, oe, p = _dust_lab(_audit_position(quantity=0.002))
    ex.filters = {"min_qty": 0.0001, "step_size": 0.0001, "min_notional": 0.1}
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert oe.sell_calls[0]["quantity"] == pytest.approx(0.0019)
    assert p.status == "DUST" and p.quantity > 0


def test_fee_lookup_failure_blocks_without_consuming_external_bnb():
    s, ex, oe, p = _dust_lab(_audit_position(quantity=0.01))
    ex.fee_rate = float("nan")
    before = dict(ex.asset_balances)
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert not oe.sell_calls and ex.asset_balances == before and p.status == "OPEN"


def test_restart_with_only_legacy_dust_classifies_without_selling(caplog):
    caplog.set_level("INFO", logger="execution.live_trading_service")
    p = _audit_position()
    ex = _DustExchange(asset_balances={"BNB":p.quantity + 0.00030331}, candles=[_current_frame()])
    store = _InMemoryPositionStore(initial_state=p)
    s, _, oe, _ = _dust_lab(p, ex, store, load_all_open_trade_states=lambda:{p.trade_id:p})
    s._is_current_closed_candle = LiveTradingService._is_current_closed_candle
    s._strategy_factory = lambda _: _ScriptedStrategy([SignalType.HOLD], exit_seq=[SignalType.SELL])
    result = s.run(LiveTradingConfig(symbol=p.symbol, timeframe=p.timeframe, strategy_name=p.strategy,
                                     strategy_version="v1", max_cycles=3))
    assert result["open_positions"] == 0 and result["dust_positions"] == 1
    assert not oe.sell_calls and store.load_all()[0].quantity == p.quantity
    assert result["capital_free"] == 100 and result["capital_committed"] == 0
    messages = [record.message for record in caplog.records]
    assert "BNB/USDT -> DUST" in messages
    assert "BNB/USDT -> HOLD" not in messages
    assert any("OPEN=0/3 DUST=1" in message and "capital_committed_usdt=0.00" in message
               for message in messages if message.startswith("LIVE_STATE |"))
    assert any("trade_id=3081 status=DUST" in message and "realized_pnl_usdt=-0.457207" in message
               for message in messages if message.startswith("LIVE_TRADE |"))
    assert sum(message.startswith("LIVE_STATE |") for message in messages) == 1


def test_terminal_summary_separates_open_and_dust_and_deduplicates(caplog):
    dust = _audit_position(status="DUST")
    active = replace(dust, trade_id=4000, status="OPEN", quantity=0.01)
    service, _, _, _ = _dust_lab(dust)
    service._dust_positions = {dust.trade_id: dust}
    active_positions = {active.context_key: active}
    committed = service._capital_committed(active_positions)

    caplog.set_level("INFO", logger="execution.live_trading_service")
    for _ in range(3):
        service._log_terminal_position_summary(
            open_positions=active_positions,
            committed_usdt=committed,
            free_usdt=28.25,
            max_open_positions=3,
        )

    summaries = [record.message for record in caplog.records if record.message.startswith("LIVE_STATE |")]
    trade_lines = [record.message for record in caplog.records if record.message.startswith("LIVE_TRADE |")]
    assert len(summaries) == 1
    assert "OPEN=1/3 DUST=1" in summaries[0]
    assert "DUST_qty=BNB:0.000987260000" in summaries[0]
    assert "capital_committed_usdt=7.87" in summaries[0]
    assert "dust_cost_usdt=0.777124" in summaries[0]
    assert "capital_free_usdt=28.25" in summaries[0]
    assert len(trade_lines) == 2
    assert any("trade_id=3081 status=DUST" in line and "realized_pnl_usdt=-0.457207" in line
               for line in trade_lines)
    assert any("trade_id=4000 status=OPEN" in line for line in trade_lines)


def test_repeat_sell_block_does_not_allocate_fee_again():
    s, _, oe, p = _dust_lab()
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    before = (p.entry_fee_allocated_usdt, p.realized_exit_fees_usdt, p.realized_net_pnl)
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert before == (p.entry_fee_allocated_usdt, p.realized_exit_fees_usdt, p.realized_net_pnl)
    assert not oe.sell_calls


def test_nonterminal_partial_fill_never_retries_or_detaches_dust():
    p = _audit_position(quantity=0.01)
    s, _, _, _ = _dust_lab(p)
    class Nonterminal(_FakeOE):
        def execute_market_sell(self, **kwargs):
            self.sell_calls.append(kwargs)
            return _Obj(status="OPEN", price=721.14, filled_quantity=0.008,
                        fee_usdt=0.001, base_fee_quantity=0)
    oe = Nonterminal()
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert p.exit_pending and p.status == "OPEN"
    assert not s._try_close_position(p, 721.14, "strategy_exit", oe)
    assert len(oe.sell_calls) == 1


def test_restart_mixed_open_and_dust_is_idempotent_and_never_sells(tmp_path):
    dust = _audit_position(status="DUST")
    active = replace(dust, trade_id=4001, status="OPEN", quantity=0.01)
    store = live_service_module._LivePositionStore(tmp_path / "live_positions.json")
    store.save_all([active, dust])
    exchange = _DustExchange(asset_balances={
        "BNB": active.quantity + dust.quantity + 0.00030331,
    })
    db_states = {active.trade_id: active, dust.trade_id: dust}
    service, _, order_executor, _ = _dust_lab(
        active,
        exchange,
        store,
        load_all_open_trade_states=lambda: db_states,
    )
    cfg = LiveTradingConfig(
        symbol=active.symbol,
        timeframe=active.timeframe,
        strategy_name=active.strategy,
        strategy_version="v1",
    )
    target = {active.context_key}
    before_balance = dict(exchange.asset_balances)

    restored = service._reconcile_on_startup(cfg, exchange, store, target)
    service._store_save_positions(store, restored)
    assert set(restored) == {active.context_key}
    assert set(service._dust_positions) == {dust.trade_id}
    assert service._dust_positions[dust.trade_id].quantity == dust.quantity

    restarted, _, restart_order_executor, _ = _dust_lab(
        active,
        exchange,
        store,
        load_all_open_trade_states=lambda: db_states,
    )
    restored_again = restarted._reconcile_on_startup(cfg, exchange, store, target)
    restarted._store_save_positions(store, restored_again)

    assert set(restored_again) == {active.context_key}
    assert set(restarted._dust_positions) == {dust.trade_id}
    assert {position.trade_id for position in store.load_all()} == {active.trade_id, dust.trade_id}
    assert not order_executor.sell_calls
    assert not restart_order_executor.sell_calls
    assert exchange.asset_balances == before_balance



def test_financial_summary_heartbeat_and_position_changes(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(live_service_module, "monotonic", lambda: clock[0])
    service, _, _, dust = _dust_lab()
    dust = replace(dust, status="DUST")
    service._dust_positions = {dust.trade_id: dust}
    caplog.set_level("INFO", logger="execution.live_trading_service")
    def emit(free=28.25577283):
        service._log_terminal_position_summary(open_positions={}, committed_usdt=0.0,
                                              free_usdt=free, max_open_positions=3)
    emit()
    for time in (15.0, 150.0, 299.99):
        clock[0] = time
        emit()
    assert sum(r.getMessage().startswith("LIVE_STATE |") for r in caplog.records) == 1
    clock[0] = 300.0
    emit()
    assert sum(r.getMessage().startswith("LIVE_STATE |") for r in caplog.records) == 2
    clock[0] = 301.0
    dust.quantity /= 2
    emit()
    assert sum(r.getMessage().startswith("LIVE_STATE |") for r in caplog.records) == 3
    clock[0] = 302.0
    emit(None)
    messages = [r.getMessage() for r in caplog.records]
    assert "capital_free_usdt=UNKNOWN balance_status=UNAVAILABLE" in messages[-2]
    assert "timeframe=4h strategy=Sma200RegimeGated" in messages[-1]
    clock[0] = 303.0
    emit()
    assert "balance_status=CURRENT" in caplog.records[-2].getMessage()


def test_summary_formatter_marks_unavailable_balance_without_invented_pnl():
    import logging
    from logging_service.formatter import build_formatter
    text = build_formatter().format(logging.LogRecord(
        "execution.live_trading_service", logging.INFO, "", 0,
        "LIVE_STATE | OPEN=0/3 DUST=1 DUST_qty=BNB:0.000987260000 "
        "capital_committed_usdt=0.00 dust_cost_usdt=0.777124 "
        "capital_free_usdt=UNKNOWN balance_status=UNAVAILABLE", (), None))
    assert "INDISPONIVEL (consulta atual falhou)" in text
    assert "resultado=" not in text
