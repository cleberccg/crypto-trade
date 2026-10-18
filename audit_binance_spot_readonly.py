"""Reconciliação Binance Spot BNB/USDT com cliente GET-only e MySQL/JSON somente leitura.

Com --env-file, le exclusivamente o arquivo explicito, sem alterar os.environ.
As credenciais podem permitir escrita: a restricao e de aplicacao/transacao.
Sem essa opcao, mantem o modo legado de credenciais dedicadas.
Nao importa settings, BinanceClient ou servico LIVE.
"""
from __future__ import annotations

import argparse
import json
import os
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine.url import make_url

from exchange.binance_spot_readonly_audit import (
    BinanceAuditConfigurationError,
    BinanceAuditRequestError,
    BinanceSpotReadOnlyAuditClient,
)


_PROJECT_ROOT = Path(__file__).resolve().parent
_APPLICATION_WARNING = (
    "Credenciais existentes podem possuir permissao de escrita. "
    "Permissoes nao verificadas: protecao somente leitura de aplicacao "
    "(GET fixos e SELECT em transacao READ ONLY), nao de credencial."
)


def _load_project_env(path: Path) -> dict[str, str]:
    try:
        # No interpolation: ${VAR} must never resolve from the invoking terminal.
        with path.open(encoding="utf-8-sig") as source:
            values = dotenv_values(stream=source, interpolate=False)
    except Exception:
        raise RuntimeError("Nao foi possivel carregar o arquivo .env explicito; detalhes omitidos.") from None
    required = ("MODE", "BINANCE_TESTNET", "DATABASE_URL", "BINANCE_API_KEY", "BINANCE_API_SECRET")
    config = {key: (values.get(key) or "").strip() for key in required}
    if any(not value or "${" in value for value in config.values()):
        raise RuntimeError("Configuracao explicita incompleta ou com interpolacao ambigua; nenhuma consulta autorizada.")
    if config["MODE"].lower() != "live" or config["BINANCE_TESTNET"].lower() not in {"false", "0", "no", "off"}:
        raise RuntimeError("O arquivo explicito deve declarar MODE=live e BINANCE_TESTNET=false (MAINNET).")
    if (values.get("EXCHANGE", "binance") or "").strip().lower() != "binance":
        raise RuntimeError("Exchange do arquivo explicito nao corresponde a Binance.")
    _validate_audit_database_url(config["DATABASE_URL"])
    return config


def _guard_readonly_sql(connection, cursor, statement, parameters, context, executemany):
    normalized = " ".join(statement.strip().upper().split())
    controls = {
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET SESSION TRANSACTION READ ONLY",
    }
    if not normalized.startswith("SELECT ") and normalized not in controls:
        raise RuntimeError("SQL nao permitido na auditoria somente leitura.")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Informe --since com timezone UTC explícito, por exemplo 2026-10-17T00:00:00Z.")
    return parsed.astimezone(timezone.utc)


def _select_assets(account: dict[str, Any]) -> dict[str, dict[str, str]]:
    balances = account.get("balances", [])
    result: dict[str, dict[str, str]] = {}
    for row in balances:
        asset = row.get("asset")
        if asset in {"BNB", "USDT"}:
            result[str(asset)] = {
                "free": str(row.get("free", "0")),
                "locked": str(row.get("locked", "0")),
            }
    return result


def _read_mysql_snapshot(database_url: str, since: datetime) -> dict[str, Any]:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "mysql" or parsed_url.database != "crypto_bot":
        raise RuntimeError("AUDIT_DATABASE_URL deve apontar ao MySQL esperado crypto_bot.")
    engine = create_engine(database_url, pool_pre_ping=True, echo=False, hide_parameters=True)
    event.listen(engine, "before_cursor_execute", _guard_readonly_sql)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            with connection.begin():
                snapshot_utc = connection.execute(text("SELECT UTC_TIMESTAMP()")).scalar_one()
                trade = connection.execute(text(
                    "SELECT id,status,is_paper,symbol,strategy_name,timeframe,quantity,original_quantity,"
                    "entry_fee_usdt,entry_fee_allocated_usdt,gross_pnl,exit_fee_usdt,total_fees_usdt,pnl,"
                    "fee_accounting_complete,exit_price,exit_reason,exit_time FROM trades WHERE id=3081"
                )).mappings().one_or_none()
                active_bnb = connection.execute(text(
                    "SELECT id,status,symbol,strategy_name,timeframe,quantity FROM trades "
                    "WHERE is_paper=0 AND symbol='BNB/USDT' AND status IN ('OPEN','DUST') ORDER BY id"
                )).mappings().all()
                live_eth = connection.execute(text(
                    "SELECT id,status,symbol,strategy_name,timeframe,quantity FROM trades "
                    "WHERE is_paper=0 AND symbol='ETH/USDT' AND status IN ('OPEN','DUST') ORDER BY id"
                )).mappings().all()
                local_orders = connection.execute(text(
                    "SELECT id,trade_id,exchange_order_id,symbol,side,status,quantity,filled_quantity,"
                    "fee,fee_currency,fee_usdt,base_fee_quantity,timestamp FROM orders "
                    "WHERE symbol='BNB/USDT' AND (timestamp>=:since OR trade_id=3081) ORDER BY timestamp,id"
                ), {"since": since.replace(tzinfo=None)}).mappings().all()
                nonterminal_count = connection.execute(text(
                    "SELECT COUNT(*) FROM orders WHERE UPPER(status) NOT IN "
                    "('CLOSED','FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED')"
                )).scalar_one()
                bnb_owned = connection.execute(text(
                    "SELECT COALESCE(SUM(quantity),0) FROM trades WHERE is_paper=0 "
                    "AND symbol='BNB/USDT' AND status IN ('OPEN','DUST')"
                )).scalar_one()
                return {
                    "snapshot_utc": snapshot_utc.isoformat(),
                    "trade_3081": dict(trade) if trade else None,
                    "active_bnb": [dict(row) for row in active_bnb],
                    "live_eth": [dict(row) for row in live_eth],
                    "local_orders": [dict(row) for row in local_orders],
                    "nonterminal_local_orders": int(nonterminal_count or 0),
                    "bot_owned_bnb": float(bnb_owned or 0.0),
                }
    except Exception:
        # SQLAlchemy exceptions can embed a DSN; don't include them in the CLI output.
        raise RuntimeError("Consulta MySQL somente leitura falhou; detalhes omitidos por segurança.") from None
    finally:
        engine.dispose()


def _read_state(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise RuntimeError(f"Não foi possível ler o arquivo de estado informado: {path.name}.") from None
    rows = data if isinstance(data, list) else [data]
    if not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("Formato de estado LIVE inválido.")
    return rows


def _validate_audit_database_url(database_url: str) -> None:
    try:
        parsed_url = make_url(database_url)
    except Exception:
        raise RuntimeError("AUDIT_DATABASE_URL inválida; detalhes omitidos por segurança.") from None
    if parsed_url.get_backend_name() != "mysql" or parsed_url.database != "crypto_bot":
        raise RuntimeError("AUDIT_DATABASE_URL deve apontar ao MySQL esperado crypto_bot.")


def reconcile(
    client: BinanceSpotReadOnlyAuditClient,
    *,
    database_url: str,
    state_path: Path,
    since: datetime,
    known_external_bnb: float,
    tolerance: float = 1e-8,
    mysql_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    end_time = datetime.now(timezone.utc)
    start_ms = int(since.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)

    account = client.fetch_account()
    open_orders = client.fetch_bnb_usdt_open_orders()
    exchange_orders = client.fetch_bnb_usdt_orders(start_ms, end_ms)
    exchange_trades = client.fetch_bnb_usdt_trades(start_ms, end_ms)
    mysql = mysql_snapshot if mysql_snapshot is not None else _read_mysql_snapshot(database_url, since)
    states = _read_state(state_path)
    state_3081 = next((row for row in states if int(row.get("trade_id", -1)) == 3081), None)

    asset_balances = _select_assets(account)
    blockers: list[str] = []
    if "BNB" not in asset_balances or "USDT" not in asset_balances:
        blockers.append("Resposta de saldo sem BNB ou USDT.")
    if open_orders:
        blockers.append("Há ordens BNB/USDT abertas na Binance.")
    if mysql["nonterminal_local_orders"]:
        blockers.append("Há ordens locais não terminais no MySQL.")

    trade = mysql["trade_3081"]
    if trade is None:
        blockers.append("Trade 3081 ausente do MySQL.")
    else:
        if trade["status"] != "DUST":
            blockers.append(f"Trade 3081 no MySQL está {trade['status']}, não DUST.")
        if int(trade["is_paper"] or 0) != 0 or trade["symbol"] != "BNB/USDT":
            blockers.append("Trade 3081 não corresponde à posição LIVE BNB/USDT esperada.")
        if state_3081 is None:
            blockers.append("Trade 3081 ausente do JSON de estado.")
        else:
            if state_3081.get("status", "OPEN") != "DUST":
                blockers.append("JSON de trade 3081 não está DUST.")
            if abs(float(state_3081.get("quantity", 0.0)) - float(trade["quantity"] or 0.0)) > tolerance:
                blockers.append("Quantidade do trade 3081 diverge entre MySQL e JSON.")
            state_pnl = state_3081.get("realized_net_pnl")
            if state_pnl is not None and trade["pnl"] is not None and abs(float(state_pnl) - float(trade["pnl"])) > 1e-8:
                blockers.append("PnL realizado do trade 3081 diverge entre MySQL e JSON.")
            persisted_pairs = (
                ("entry_fee_usdt", "entry_fee_usdt"),
                ("entry_fee_allocated_usdt", "entry_fee_allocated_usdt"),
                ("realized_gross_pnl", "gross_pnl"),
                ("realized_exit_fees_usdt", "exit_fee_usdt"),
            )
            for state_key, db_key in persisted_pairs:
                state_value = state_3081.get(state_key)
                db_value = trade.get(db_key)
                if state_value is not None and db_value is not None and abs(float(state_value) - float(db_value)) > 1e-8:
                    blockers.append(f"Campo {state_key} do trade 3081 diverge entre MySQL e JSON.")
            if bool(state_3081.get("fee_accounting_complete", False)) != bool(trade.get("fee_accounting_complete", False)):
                blockers.append("Estado de completude de fees do trade 3081 diverge entre MySQL e JSON.")
        if trade["exit_price"] is not None or trade["exit_reason"] is not None or trade["exit_time"] is not None:
            blockers.append("Trade DUST tem campos de fechamento preenchidos inesperadamente.")

    if "BNB" in asset_balances:
        bnb_free = float(asset_balances["BNB"]["free"])
        bnb_locked = float(asset_balances["BNB"]["locked"])
        bnb_total = bnb_free + bnb_locked
        expected_bnb = mysql["bot_owned_bnb"] + known_external_bnb
        if abs(bnb_total - expected_bnb) > tolerance:
            blockers.append("Saldo BNB total difere de ownership MySQL agregado + BNB externo documentado.")
        if bnb_locked > tolerance and not open_orders:
            blockers.append("Há BNB bloqueado sem ordem aberta BNB/USDT correspondente.")
    else:
        bnb_free = bnb_locked = bnb_total = 0.0

    local_order_ids = {
        str(row["exchange_order_id"])
        for row in mysql["local_orders"]
        if row.get("exchange_order_id")
        and row.get("timestamp") is not None
        and row["timestamp"] >= since.replace(tzinfo=None)
    }
    exchange_order_ids = {str(row.get("orderId")) for row in exchange_orders if row.get("orderId") is not None}
    unknown_order_ids = sorted(exchange_order_ids - local_order_ids)
    if unknown_order_ids:
        blockers.append("Histórico Binance contém orderId sem registro local correspondente.")
    missing_exchange_order_ids = sorted(local_order_ids - exchange_order_ids)
    if missing_exchange_order_ids:
        blockers.append("Há orderId local recente ausente no histórico consultado da Binance.")

    local_order_totals: dict[str, dict[str, Any]] = {}
    for row in mysql["local_orders"]:
        order_id = str(row.get("exchange_order_id") or "")
        if not order_id or row.get("timestamp") is None or row["timestamp"] < since.replace(tzinfo=None):
            continue
        summary = local_order_totals.setdefault(
            order_id,
            {"side": str(row.get("side") or "").upper(), "filled": 0.0, "statuses": set()},
        )
        summary["filled"] += float(row.get("filled_quantity") or 0.0)
        summary["statuses"].add(str(row.get("status") or "").upper())
    order_mismatches = []
    terminal_equivalence = {"CLOSED": "FILLED", "FILLED": "FILLED", "CANCELED": "CANCELED", "CANCELLED": "CANCELED"}
    for remote in exchange_orders:
        order_id = str(remote.get("orderId") or "")
        local = local_order_totals.get(order_id)
        if local is None:
            continue
        remote_side = str(remote.get("side") or "").upper()
        remote_status = str(remote.get("status") or "").upper()
        normalized_status = terminal_equivalence.get(remote_status, remote_status)
        local_statuses = {terminal_equivalence.get(value, value) for value in local["statuses"]}
        remote_filled = float(remote.get("executedQty") or 0.0)
        if (
            local["side"] != remote_side
            or abs(local["filled"] - remote_filled) > tolerance
            or (local_statuses and normalized_status not in local_statuses)
        ):
            order_mismatches.append(order_id)
    if order_mismatches:
        blockers.append("Lado/status/quantidade executada de ordens Binance diverge do MySQL.")

    local_fill_totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"quantity": 0.0, "fee": 0.0})
    for row in mysql["local_orders"]:
        order_id = str(row.get("exchange_order_id") or "")
        if (
            not order_id
            or row.get("timestamp") is None
            or row["timestamp"] < since.replace(tzinfo=None)
        ):
            continue
        currency = str(row.get("fee_currency") or "")
        key = (order_id, currency)
        local_fill_totals[key]["quantity"] += float(row.get("filled_quantity") or 0.0)
        local_fill_totals[key]["fee"] += float(row.get("fee") or 0.0)

    exchange_fill_totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"quantity": 0.0, "fee": 0.0})
    for row in exchange_trades:
        order_id = str(row.get("orderId") or "")
        currency = str(row.get("commissionAsset") or "")
        key = (order_id, currency)
        exchange_fill_totals[key]["quantity"] += float(row.get("qty") or 0.0)
        exchange_fill_totals[key]["fee"] += float(row.get("commission") or 0.0)
    fill_mismatches = []
    for key in sorted(set(local_fill_totals) | set(exchange_fill_totals)):
        local = local_fill_totals.get(key, {"quantity": 0.0, "fee": 0.0})
        remote = exchange_fill_totals.get(key, {"quantity": 0.0, "fee": 0.0})
        if abs(local["quantity"] - remote["quantity"]) > tolerance or abs(local["fee"] - remote["fee"]) > tolerance:
            fill_mismatches.append({"order_id": key[0], "fee_asset": key[1]})
    if fill_mismatches:
        blockers.append("Quantidade/taxa dos fills Binance diverge dos registros locais.")

    relevant_orders = [
        {key: row.get(key) for key in ("orderId", "symbol", "side", "status", "origQty", "executedQty", "price", "time", "updateTime")}
        for row in exchange_orders
    ]
    relevant_trades = [
        {key: row.get(key) for key in ("id", "orderId", "symbol", "isBuyer", "price", "qty", "quoteQty", "commission", "commissionAsset", "time")}
        for row in exchange_trades
    ]
    return {
        "decision": "LIBERADO PARA REINÍCIO" if not blockers else "BLOQUEADO",
        "blockers": blockers,
        "audited_from_utc": since.isoformat(),
        "audited_to_utc": end_time.isoformat(),
        "binance_assets": asset_balances,
        "bnb_total": bnb_total,
        "bnb_bot_owned_mysql": mysql["bot_owned_bnb"],
        "bnb_external_declared": known_external_bnb,
        "usdt_free": float(asset_balances.get("USDT", {}).get("free", 0.0)),
        "open_orders": [{key: row.get(key) for key in ("orderId", "symbol", "side", "status", "origQty", "executedQty", "price", "time") } for row in open_orders],
        "orders_since_start_count": len(relevant_orders),
        "fills_since_start_count": len(relevant_trades),
        "recent_orders": relevant_orders[-20:],
        "recent_fills": relevant_trades[-20:],
        "unknown_exchange_order_ids": unknown_order_ids,
        "local_order_ids_missing_on_exchange": missing_exchange_order_ids,
        "order_mismatches": sorted(order_mismatches),
        "fill_mismatches": fill_mismatches,
        "mysql_snapshot_utc": mysql["snapshot_utc"],
        "trade_3081": {
            "mysql_status": trade.get("status") if trade else None,
            "mysql_quantity": trade.get("quantity") if trade else None,
            "mysql_realized_pnl_usdt": trade.get("pnl") if trade else None,
            "mysql_total_fees_usdt": trade.get("total_fees_usdt") if trade else None,
            "json_status": state_3081.get("status", "OPEN") if state_3081 else None,
            "json_quantity": state_3081.get("quantity") if state_3081 else None,
            "json_realized_pnl_usdt": state_3081.get("realized_net_pnl") if state_3081 else None,
        },
        "live_eth_positions_preserved": mysql["live_eth"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=_parse_utc, required=True, help="Início UTC inclusivo, ex. 2026-10-17T00:00:00Z")
    parser.add_argument("--known-external-bnb", type=float, required=True, help="Saldo externo BNB previamente documentado; não será atribuído ao bot")
    parser.add_argument("--state", type=Path, default=_PROJECT_ROOT / "optimization/results/live_positions.json")
    parser.add_argument("--env-file", type=Path, help="Arquivo explicito do projeto; ignora configuracao e credenciais do terminal")
    parser.add_argument("--confirm-read-only-credentials", action="store_true", help="Confirma que a chave Binance dedicada tem somente Enable Reading e que o usuário MySQL é somente leitura")
    args = parser.parse_args(argv)
    if not args.env_file and not args.confirm_read_only_credentials:
        parser.error("Confirme a permissão somente leitura com --confirm-read-only-credentials.")
    if not math.isfinite(args.known_external_bnb) or args.known_external_bnb < 0:
        parser.error("--known-external-bnb não pode ser negativo.")
    config = None
    if args.env_file:
        try:
            config = _load_project_env(args.env_file)
        except RuntimeError as exc:
            print(json.dumps({"decision": "BLOQUEADO", "blockers": [str(exc), "Nenhuma consulta foi feita."]}, ensure_ascii=False))
            return 2
        print(_APPLICATION_WARNING, file=sys.stderr)
    database_url = config["DATABASE_URL"] if config else os.environ.get("AUDIT_DATABASE_URL", "").strip()
    if not database_url:
        print(json.dumps({"decision": "BLOQUEADO", "blockers": ["AUDIT_DATABASE_URL de usuário MySQL somente leitura não configurada; nenhuma chamada Binance foi feita."]}, ensure_ascii=False, indent=2))
        return 2
    try:
        _validate_audit_database_url(database_url)
        _read_state(args.state)
    except RuntimeError as exc:
        print(json.dumps({"decision": "BLOQUEADO", "blockers": [str(exc), "Nenhuma chamada Binance foi feita."]}, ensure_ascii=False, indent=2))
        return 2
    try:
        client = (
            BinanceSpotReadOnlyAuditClient(config["BINANCE_API_KEY"], config["BINANCE_API_SECRET"])
            if config else BinanceSpotReadOnlyAuditClient.from_environment()
        )
    except BinanceAuditConfigurationError as exc:
        print(json.dumps({"decision": "BLOQUEADO", "blockers": [str(exc), "Nenhuma chamada Binance foi feita."]}, ensure_ascii=False, indent=2))
        return 2
    try:
        # O MySQL somente leitura é validado antes de qualquer request privada à Binance.
        mysql_snapshot = _read_mysql_snapshot(database_url, args.since)
    except Exception:
        client.close()
        print(json.dumps({"decision": "BLOQUEADO", "blockers": ["Snapshot MySQL somente leitura indisponível; nenhuma chamada Binance foi feita."]}, ensure_ascii=False, indent=2))
        return 2
    try:
        report = reconcile(
            client,
            database_url=database_url,
            state_path=args.state,
            since=args.since,
            known_external_bnb=args.known_external_bnb,
            mysql_snapshot=mysql_snapshot,
        )
    except (RuntimeError, BinanceAuditRequestError) as exc:
        print(json.dumps({"decision": "BLOQUEADO", "blockers": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    except Exception:
        print(json.dumps({"decision": "BLOQUEADO", "blockers": ["Falha inesperada na reconciliação; detalhes omitidos por segurança."]}, ensure_ascii=False, indent=2))
        return 2
    finally:
        client.close()
    report["credential_protection"] = _APPLICATION_WARNING if config else "Credenciais dedicadas declaradas pelo operador; permissoes nao verificadas pela auditoria."
    report["configuration_source"] = "explicit_env_file" if config else "dedicated_environment"
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["decision"] == "LIBERADO PARA REINÍCIO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
