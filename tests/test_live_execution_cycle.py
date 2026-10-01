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
