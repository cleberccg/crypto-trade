"""Read-only LIVE projections for the existing dashboard. No trading client imports."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

import psutil
from sqlalchemy import create_engine, event, text

from webapi.transaction_costs import transaction_costs
from audit_binance_spot_readonly import _guard_readonly_sql, _load_project_env
from exchange.binance_spot_readonly_audit import BinanceSpotReadOnlyAuditClient

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def iso(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.astimezone(UTC).isoformat()
    return value


def process_snapshot(root=ROOT):
    result = {"worker": [], "daemon": []}
    for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if str(process.info.get("name", "")).lower() not in {"python.exe", "pythonw.exe", "python", "python3"}:
                continue
            argv = process.info["cmdline"] or []
            if not any(Path(arg).name.lower() == "main.py" for arg in argv):
                continue
            if Path(process.cwd()).resolve() != root.resolve():
                continue
            command = next((argv[i + 1] for i, arg in enumerate(argv[:-1]) if Path(arg).name.lower() == "main.py"), "")
            key = "worker" if command == "live" else "daemon" if command == "market-data-daemon" else None
            if key:
                result[key].append({"pid": process.info["pid"], "started_at": datetime.fromtimestamp(process.info["create_time"], UTC).isoformat()})
        except (psutil.Error, OSError):
            continue
    return result


def read_log_activity(path, processes, now):
    """Read a bounded tail; never send raw logs, command lines or exceptions to the browser."""
    result = {"worker_at": None, "daemon_at": None, "state_at": None, "decision": None,
              "decision_at": None, "signal": None, "signal_at": None, "errors": []}
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 2_000_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return result
    # The operational formatter uses local wall time, without a UTC offset.
    from zoneinfo import ZoneInfo
    local = ZoneInfo("America/Sao_Paulo")
    worker_start = min((p["started_at"] for p in processes["worker"]), default=now.isoformat())
    daemon_start = min((p["started_at"] for p in processes["daemon"]), default=now.isoformat())
    for line in lines:
        fields = line.split(" | ", 4)
        if len(fields) != 5:
            continue
        try:
            stamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=local).astimezone(UTC).isoformat()
        except ValueError:
            continue
        logger = fields[2].strip()
        message = fields[4]
        if logger == "market_data.daemon" and stamp >= daemon_start:
            result["daemon_at"] = stamp
        if logger in {"execution.live_trading_service", "exchange.binance_client"} and stamp >= worker_start:
            result["worker_at"] = stamp
            if "LIVE_STATE |" in message:
                result["state_at"] = stamp
            action = re.search(r"BNB/USDT -> ([A-Z_]+)", message)
            if action:
                result["decision"], result["decision_at"] = action[1], stamp
            signal = re.search(r"signal=(BUY|SELL|HOLD).*?reason=([^|]+)", message)
            if signal:
                result["signal"] = {"signal": signal[1], "reason": signal[2].strip()[:200]}
                result["signal_at"] = stamp
        if fields[1].strip() in {"ERROR", "CRITICAL"} and stamp >= worker_start:
            result["errors"].append({"at": stamp, "source": logger, "message": "Erro operacional registrado; consultar terminal local."})
    result["errors"] = result["errors"][-10:]
    return result


def decimal_sum(rows, field):
    return float(sum((Decimal(str(row[field])) for row in rows if row.get(field) is not None), Decimal(0)))


class LiveDashboardReader:
    def __init__(self, root=ROOT):
        self.root = root
        self._engine = None
        self._account_lock = threading.Lock()
        self._account_until = 0.0
        self._account = None
        self._account_at = None
        self._open_orders = None

    def _database(self):
        if self._engine is None:
            config = _load_project_env(self.root / ".env")
            self._engine = create_engine(config["DATABASE_URL"], echo=False, hide_parameters=True,
                                         pool_pre_ping=True, pool_size=2, max_overflow=1)
            event.listen(self._engine, "before_cursor_execute", _guard_readonly_sql)
        return self._engine

    def _read_db(self, strategy, symbol, timeframe, since=None, until=None):
        with self._database().connect() as conn:
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            conn.commit()
            with conn.begin():
                stamp = conn.execute(text("SELECT UTC_TIMESTAMP()")).scalar_one()
                positions = [{key: iso(value) for key, value in row.items()} for row in conn.execute(text(
                    "SELECT id,symbol,strategy_name,timeframe,status,quantity,entry_price,stake_amount,"
                    "pnl,total_fees_usdt,fee_accounting_complete,entry_time,exit_time,exit_price,exit_reason "
                    "FROM trades WHERE is_paper=0 AND status IN ('OPEN','DUST') ORDER BY id"
                )).mappings()]
                where = "t.is_paper=0"
                params = {}
                for key, value in (("strategy_name", strategy), ("symbol", symbol), ("timeframe", timeframe)):
                    if value:
                        where += f" AND t.{key}=:{key}"
                        params[key] = value
                date_where = ""
                if since:
                    date_where += " AND COALESCE(t.exit_time,t.entry_time)>=:since"
                    params["since"] = since
                if until:
                    date_where += " AND COALESCE(t.exit_time,t.entry_time)<=:until"
                    params["until"] = until
                history = [{key: iso(value) for key, value in row.items()} for row in conn.execute(text(
                    "SELECT t.id,t.symbol,t.strategy_name,t.timeframe,t.status,t.quantity,t.entry_price,"
                    "t.stake_amount,t.pnl,t.total_fees_usdt,t.fee_accounting_complete,t.entry_time,t.exit_time,"
                    "t.exit_price,t.exit_reason FROM trades t WHERE " + where + date_where +
                    " ORDER BY t.id DESC LIMIT 200"
                ), params).mappings()]
                order_params = {k: v for k, v in params.items() if k not in {"since", "until"}}
                order_where = where
                if since:
                    order_where += " AND o.timestamp>=:since"
                    order_params["since"] = since
                if until:
                    order_where += " AND o.timestamp<=:until"
                    order_params["until"] = until
                orders = [{key: iso(value) for key, value in row.items()} for row in conn.execute(text(
                    "SELECT o.id,o.trade_id,o.exchange_order_id,o.symbol,o.side,o.status,o.quantity,"
                    "o.filled_quantity,o.price,o.fee,o.fee_currency,o.fee_usdt,o.fee_source,o.fee_conversion_price,o.timestamp,"
                    "t.strategy_name,t.timeframe FROM orders o JOIN trades t ON t.id=o.trade_id WHERE " +
                    order_where + " ORDER BY o.id DESC LIMIT 200"
                ), order_params).mappings()]
                orders = [transaction_costs(row) for row in orders]
                pnl_rows = conn.execute(text(
                    "SELECT t.pnl FROM trades t WHERE " + where +
                    " AND t.status IN ('OPEN','DUST','CLOSED') AND t.pnl IS NOT NULL"
                ), {k: v for k, v in params.items() if k not in {"since", "until"}}).scalars().all()
                candle = conn.execute(text(
                    "SELECT MAX(open_time) FROM candles WHERE symbol=:symbol AND timeframe=:timeframe "
                    "AND open_time<=:closed_before"
                ), {"symbol": symbol or "BNB/USDT", "timeframe": timeframe or "4h",
                    "closed_before": datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4)}).scalar_one()
                return {"at": iso(stamp), "positions": positions, "history": history, "orders": orders,
                        "realized_pnl": float(sum((Decimal(str(v)) for v in pnl_rows), Decimal(0))) if pnl_rows else None,
                        "closed_candle": iso(candle)}

    def _read_account(self):
        # A separate fixed-GET client, never ccxt or the trading client. Cache avoids polling the exchange per browser request.
        with self._account_lock:
            if monotonic() < self._account_until:
                return self._account, self._account_at
            try:
                config = _load_project_env(self.root / ".env")
                client = BinanceSpotReadOnlyAuditClient(config["BINANCE_API_KEY"], config["BINANCE_API_SECRET"], timeout=8)
                try:
                    account = client.fetch_account()
                    open_orders = client.fetch_bnb_usdt_open_orders()
                finally:
                    client.close()
                self._account = {r["asset"]: {"free": str(r["free"]), "locked": str(r["locked"])}
                                 for r in account["balances"] if r.get("asset") in {"BNB", "USDT"}}
                self._account_at = datetime.now(UTC).isoformat()
                self._open_orders = [{k: row.get(k) for k in ("orderId", "symbol", "side", "status", "origQty", "executedQty")} for row in open_orders]
            except Exception:
                self._account = None
                self._account_at = None
                self._open_orders = None
            self._account_until = monotonic() + 30
            return self._account, self._account_at

    def snapshot(self, strategy="Sma200RegimeGated", symbol="BNB/USDT", timeframe="4h", since=None, until=None):
        now = datetime.now(UTC)
        processes = process_snapshot(self.root)
        activity = read_log_activity(self.root / "logs/application.log", processes, now)
        alerts = []
        try:
            db = self._read_db(strategy, symbol, timeframe, since, until)
        except Exception:
            db = None
            alerts.append("MySQL indisponível; dados financeiros do banco não foram substituídos por simulação.")
        account, account_at = self._read_account()
        if self._open_orders:
            alerts.append("Existem ordens BNB/USDT abertas na Binance; conferir status antes de qualquer acao operacional.")
        if account is None:
            alerts.append("Saldo Binance indisponível; nenhum saldo histórico foi apresentado como atual.")
        try:
            states = json.loads((self.root / "optimization/results/live_positions.json").read_text(encoding="utf-8"))
            if not isinstance(states, list):
                states = [states]
        except Exception:
            states = None
            alerts.append("Estado persistido indisponível.")
        context = lambda r: (not strategy or r.get("strategy_name", r.get("strategy")) == strategy) and (not symbol or r["symbol"] == symbol) and (not timeframe or r.get("timeframe") == timeframe)
        positions = [p for p in db["positions"] if context(p)] if db else []
        legacy = [p for p in db["positions"] if not context(p)] if db else []
        opened = [p for p in positions if p["status"] == "OPEN"]
        dust = [p for p in positions if p["status"] == "DUST"]
        if db and states is not None:
            for p in positions:
                state = next((s for s in states if s.get("trade_id") == p["id"]), None)
                if state is None or state.get("status", "OPEN") != p["status"] or abs(float(state["quantity"]) - float(p["quantity"])) > 1e-8:
                    alerts.append(f"Divergência MySQL/JSON no trade {p['id']}.")
                elif state.get("realized_net_pnl") is not None and p.get("pnl") is not None and abs(float(state["realized_net_pnl"]) - float(p["pnl"])) > 1e-8:
                    alerts.append(f"Divergencia de PnL realizado MySQL/JSON no trade {p['id']}.")
                if state and state.get("exit_pending"):
                    alerts.append(f"Trade {p['id']} possui intenção de saída pendente.")
        # These are costs, not mark-to-market equity. External assets and DUST are excluded.
        committed = float(sum((Decimal(str(p["quantity"])) * Decimal(str(p["entry_price"])) for p in opened), Decimal(0))) if db else None
        dust_cost = float(sum((Decimal(str(p["quantity"])) * Decimal(str(p["entry_price"])) for p in dust), Decimal(0))) if db else None
        free = float(account["USDT"]["free"]) if account and "USDT" in account else None
        from dotenv import dotenv_values
        public_config = dotenv_values(self.root / ".env", interpolate=False)
        for name in ("worker", "daemon"):
            if len(processes[name]) != 1:
                alerts.append(f"{name}: esperada uma instância; encontradas {len(processes[name])}.")
            last = activity[f"{name}_at"]
            if not last or (now - datetime.fromisoformat(last)).total_seconds() > 90:
                alerts.append(f"Atividade de {name} indisponível ou desatualizada.")
        return {
            "updated_at": datetime.now(UTC).isoformat(), "mode": "LIVE", "read_only": True,
            "context": {"strategy": strategy, "symbol": symbol, "timeframe": timeframe, "version": "v1"},
            "processes": processes, "activity": activity,
            "sources": {"mysql": {"status": "connected" if db else "unavailable", "at": db["at"] if db else None},
                        "binance": {"status": "connected" if account else "unavailable", "at": account_at},
                        "state": {"status": "available" if states is not None else "unavailable"}},
            "capital": {"usdt_free": free, "committed_open": committed, "dust_cost": dust_cost,
                        "tracked_cost_capital": free + committed if free is not None and committed is not None else None,
                        "realized_pnl": db["realized_pnl"] if db else None, "unrealized_pnl": None},
            "binance_open_orders": self._open_orders,
            "balances": account, "open_limit": int(public_config.get("MAX_OPEN_POSITIONS") or 3),
            "open_positions": opened if db else None, "dust_positions": dust if db else None,
            "preserved_positions": legacy if db else None,
            "history": db["history"] if db else None, "orders": db["orders"] if db else None,
            "last_closed_candle_available": db["closed_candle"] if db else None,
            "last_evaluated_position_candle": max((s.get("last_evaluated_candle", "") for s in (states or []) if context(s)), default=None) or None,
            "indicators": None, "alerts": alerts,
            "scope_note": "Capital = USDT livre da conta + custo OPEN do contexto; não é patrimônio a mercado nem ownership de depósitos USDT. DUST e ativos externos separados. PnL realizado por trade, sem somar novamente fills; histórico limitado aos 200 registros mais recentes por filtro.",
        }


reader = LiveDashboardReader()
