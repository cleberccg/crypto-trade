"""Auditoria de prontidão LIVE + DRY RUN para um contexto live. NUNCA envia ordens.

Somente chamadas à Binance de leitura (saldo, mercados, candles, ticker, ordens abertas, operações próprias).
A criação de ordens é interceptada por DryRunExchange e as ordens reais permanecem desarmadas
(proteção de exchange.binance_client). As gravações no banco do fluxo live são substituídas por objetos simulados.
As credenciais nunca são exibidas.

Exemplo:
    python audit_live_readiness.py --symbol BNB/USDT --timeframe 4h --strategy Sma200RegimeGated
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
from sqlalchemy import text

import exchange.binance_client as bc
import execution.order_executor as order_executor_module
from config.settings import settings
from database.connection import get_session
from execution.live_trading_service import LiveTradingConfig, LiveTradingService
from research.external_strategy_replication_strategies import (
    NEVER_STOP_FRACTION,
    NEVER_TP_MULTIPLE,
    NEVER_TRAILING_PCT,
)
from strategies.base_strategy import SignalType, StrategySignal


class DryRunExchange(bc.BinanceClient):
    """Chamadas reais somente de leitura; a criação de ordens é capturada e nunca encaminhada."""

    def __init__(self) -> None:
        super().__init__()
        self.would_send: list[dict] = []

    def create_market_order(self, symbol, side, quantity):
        amount = float(self._client.amount_to_precision(symbol, quantity))
        price = float(self._client.fetch_ticker(symbol)["last"])
        self.would_send.append({"SIDE": side.upper(), "SYMBOL": symbol, "QUANTITY": amount, "ORDER_TYPE": "MARKET",
                                "ESTIMATED_NOTIONAL": round(amount * price, 6)})
        return {"id": f"DRYRUN-{len(self.would_send)}", "status": "closed", "average": price, "filled": amount,
                "fee": {"cost": amount * price * 0.001, "currency": "USDT"}}

    def create_limit_order(self, *args, **kwargs):
        raise RuntimeError("limit orders are not allowed in dry run")


class _MemStore:
    def __init__(self, _path=None):
        self.states = []

    def save_all(self, states):
        self.states = list(states)

    def load_all(self):
        return list(self.states)


@contextmanager
def _no_db_session():
    class _Session:
        def add(self, _obj):
            return None

    yield _Session()


def _fake_db_ops(log: list) -> dict:
    counter = [0]

    def create_trade(**kw):
        counter[0] += 1
        log.append(("create_trade", kw["side"]))
        return 900_000_000 + counter[0]

    return {
        "create_trade": create_trade,
        "update_after_buy": lambda **kw: log.append(("update_after_buy", kw["fill_qty"])),
        "cancel_trade": lambda **kw: log.append(("cancel_trade", kw["trade_id"])),
        "close_trade": lambda **kw: log.append(("close_trade", kw["exit_reason"])),
    }


def audit_open_db_positions(ex: DryRunExchange, balance: dict) -> list[dict]:
    """Compara, somente para leitura, as operações live OPEN do banco com as ordens e o saldo atual da Binance."""
    out = []
    with get_session() as session:
        trades = session.execute(text(
            "SELECT id,symbol,strategy_name,timeframe,quantity,entry_price,stake_amount,entry_time,is_paper "
            "FROM trades WHERE status='OPEN'")).fetchall()
        for t in trades:
            orders = session.execute(text(
                "SELECT side,status,price,quantity,filled_quantity,fee,timestamp FROM orders WHERE trade_id=:i "
                "ORDER BY timestamp"), {"i": t[0]}).fetchall()
            asset = str(t[1]).split("/")[0]
            total = float((balance.get("total") or {}).get(asset, 0.0) or 0.0)
            price = float(ex.fetch_ticker(t[1]).get("last") or 0.0)
            try:
                recent = ex._client.fetch_my_trades(t[1], limit=10)
                last_fills = [{"side": r["side"], "amount": r["amount"], "price": r["price"],
                               "datetime": r["datetime"]} for r in recent[-5:]]
            except Exception as exc:
                last_fills = f"unavailable: {type(exc).__name__}"
            out.append({
                "trade_id": t[0], "symbol": t[1], "strategy": t[2], "timeframe": t[3], "is_paper": bool(t[8]),
                "db_quantity": float(t[4] or 0.0), "db_entry_price": float(t[5] or 0.0),
                "db_stake": float(t[6] or 0.0), "db_entry_time": str(t[7]),
                "orders": [tuple(str(x) for x in o) for o in orders],
                "binance_total_balance": total, "estimated_value_usdt": round(total * price, 4),
                "binance_recent_fills": last_fills,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="BNB/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--strategy", default="Sma200RegimeGated")
    parser.add_argument("--strategy-version", default="v1")
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    order_executor_module.get_session = _no_db_session  # A execução simulada nunca persiste registros Order

    symbol, tf, strategy_name = args.symbol, args.timeframe, args.strategy
    ctx = (symbol, strategy_name, tf)
    out: dict = {"MODE": settings.trading.mode, "BINANCE_TESTNET": settings.binance.testnet,
                 "SMALL_ACCOUNT_MODE": settings.small_account_mode,
                 "REAL_ORDERS_ARMED_BEFORE": bc.real_orders_armed()}
    try:
        bc.BinanceClient().create_market_order(symbol, "buy", 0.01)
        out["GUARD_BLOCKS_UNARMED_ORDER"] = False
    except RuntimeError as exc:
        out["GUARD_BLOCKS_UNARMED_ORDER"] = "not armed" in str(exc)

    ex = DryRunExchange()
    ex.connect()
    db_log: list = []
    try:
        out["BINANCE_DEFAULT_TYPE"] = ex._client.options.get("defaultType")
        balance = ex.fetch_balance()
        free_usdt = float((balance.get("free") or {}).get("USDT", 0.0) or 0.0)
        filters = ex.fetch_symbol_trading_filters(symbol)
        out["AVAILABLE_USDT"] = free_usdt
        out["FILTERS"] = filters
        out["OPEN_ORDERS"] = len(ex.fetch_open_orders(symbol))

        svc = LiveTradingService(base_dir=Path(os.getcwd()), exchange_factory=lambda: ex,
                                 position_store_factory=_MemStore, db_ops=_fake_db_ops(db_log),
                                 sleep_fn=lambda _s: None)
        cfg = LiveTradingConfig(symbol=symbol, timeframe=tf, strategy_name=strategy_name,
                                strategy_version=args.strategy_version, symbols=(symbol,), max_cycles=1)
        out["UNTRACKED_ASSET_BLOCKS_BUY"] = svc._has_untracked_asset_balance(ex, symbol)

        startup = svc._initialize_runtime(cfg=cfg, exchange=ex)
        frame, strategy = startup["frame"], startup["strategy"]
        last_ts = frame.index[-1]
        out["LATEST_CLOSED_CANDLE"] = str(last_ts)
        out["LATEST_CANDLE_IS_CLOSED"] = bool(last_ts + pd.Timedelta(tf) <= pd.Timestamp.now(tz="UTC"))
        enriched = strategy.calculate(frame)
        last = enriched.iloc[-1]
        out["REGIME"] = "TRENDING_BULL" if bool(last.get("regime_bull", False)) else "CASH"
        entry = strategy.entry_signal(enriched)
        out["CURRENT_DECISION"] = "LONG" if entry.signal == SignalType.BUY else "CASH"

        # Comparação de dimensionamento: stake científico do RiskManager versus o piso de SMALL_ACCOUNT_MODE (configuração inalterada).
        # Quando o regime não é de alta, usa-se um BUY sintético com a convenção de stop/TP da estratégia congelada.
        price = float(last["close"])
        if entry.signal == SignalType.BUY:
            signal = entry
        else:
            signal = StrategySignal(SignalType.BUY, price, last_ts.to_pydatetime(), score=1.0,
                                    stop_loss=price * NEVER_STOP_FRACTION, take_profit=price * NEVER_TP_MULTIPLE,
                                    trailing_stop_pct=NEVER_TRAILING_PCT)
        scientific = startup["risk_manager"].evaluate_trade(
            portfolio_value=free_usdt, entry_price=price, stop_loss=signal.stop_loss,
            take_profit=signal.take_profit, trailing_stop_pct=signal.trailing_stop_pct, strategy_score=1.0)
        fraction = float(scientific.stake_amount) / free_usdt if free_usdt > 0 else 0.0
        operational_floor = max(filters["min_notional"] * (1.0 + max(0.01, settings.trading.min_notional_buffer_pct)),
                                settings.trading.min_operational_stake_usdt)
        rounded_floor_qty = startup["live_risk_service"]._ceil_to_step(
            max(operational_floor / price, filters["min_qty"]), filters["step_size"])
        rounded_floor = rounded_floor_qty * price
        out["SIZING"] = {
            "SCIENTIFIC_POSITION_SIZE": round(float(scientific.stake_amount), 6),
            "SCIENTIFIC_POSITION_PERCENT_BALANCE": round(100 * fraction, 4),
            "BINANCE_MIN_NOTIONAL": filters["min_notional"],
            "SMALL_ACCOUNT_OPERATIONAL_FLOOR": operational_floor,
            "MIN_BALANCE_FOR_BINANCE_MIN_NOTIONAL": round(filters["min_notional"] / fraction, 2) if fraction else None,
            "MIN_BALANCE_BEFORE_LOT_ROUNDING": round(operational_floor / fraction, 2) if fraction else None,
            "ESTIMATED_MINIMUM_BALANCE_FOR_NORMAL_SIZING": round(rounded_floor / fraction, 2) if fraction else None,
        }

        restored = svc._reconcile_on_startup(cfg=cfg, exchange=ex, store=_MemStore(), target_contexts={ctx})
        out["RECONCILE_RESTORED_ON_STARTUP"] = len(restored)

        # BUY em simulação -> recuperação após reinício -> SELL em simulação.
        position = svc._try_open_position(cfg=cfg, signal=signal, live_risk_service=startup["live_risk_service"],
                                          risk_manager=startup["risk_manager"], exchange=ex,
                                          available_capital=free_usdt)
        out["DRY_RUN_BUY"] = "PASS" if position is not None else "FAIL"
        if position is not None:
            out["SIZING"]["SMALL_ACCOUNT_POSITION_SIZE"] = round(position.stake_amount, 6)
            out["SIZING"]["SMALL_ACCOUNT_PERCENT_BALANCE"] = round(100 * position.stake_amount / free_usdt, 4)
            store = _MemStore()
            store.save_all([position])
            svc2 = LiveTradingService(base_dir=Path(os.getcwd()), exchange_factory=lambda: ex,
                                      position_store_factory=_MemStore,
                                      db_ops={**_fake_db_ops(db_log),
                                              "load_all_open_trade_states": lambda: {position.trade_id: position}},
                                      sleep_fn=lambda _s: None)
            recovered = svc2._reconcile_on_startup(cfg=cfg, exchange=ex, store=store, target_contexts={ctx})
            out["RESTART_RECOVERS_OPEN_POSITION"] = ctx in recovered
            out["DUPLICATE_BUY_BLOCKED_BY_CONTEXT"] = ctx in recovered
            ok = svc._try_close_position(position=position, close_price=price, reason="regime_not_bull",
                                         order_executor=startup["order_executor"])
            out["DRY_RUN_SELL"] = "PASS" if ok else "FAIL"
        out["WOULD_SEND"] = ex.would_send
        out["FAKE_DB_OPS"] = db_log
        out["OPEN_DB_POSITIONS"] = audit_open_db_positions(ex, balance)
    finally:
        ex.disconnect()
    out["REAL_ORDERS_ARMED_AFTER"] = bc.real_orders_armed()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
