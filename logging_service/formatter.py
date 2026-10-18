from __future__ import annotations

import copy
import logging
import re


# Templates de log operacional traduzidos sem alterar os dados nem a lógica dos emissores.
# Tokens entre colchetes angulares são campos estáveis de integração/API e permanecem intactos.
_PT_BR_TEMPLATES = {
    "BinanceClient connected in TESTNET mode.": "BinanceClient conectado em modo TESTNET.",
    "BinanceClient connected in LIVE mode.": "BinanceClient conectado em modo LIVE.",
    "Markets loaded - %d symbols available.": "Mercados carregados: %d símbolos disponíveis.",
    "BinanceClient disconnected.": "BinanceClient desconectado.",
    "fetch_ohlcv - symbol=%s timeframe=%s since=%s limit=%s": "Consulta de candles (fetch_ohlcv) | symbol=%s timeframe=%s since=%s limit=%s",
    "fetch_ohlcv returned empty data for %s/%s.": "A Binance não retornou candles para %s/%s.",
    "fetch_ticker - symbol=%s last=%s": "Consulta de cotação (fetch_ticker) | symbol=%s last=%s",
    "Binance returned timestamp/recvWindow error on fetch_balance; syncing clock and retrying once.": "A Binance retornou erro de timestamp/recvWindow em fetch_balance; sincronizando relógio e tentando novamente uma vez.",
    "fetch_balance - total currencies: %d": "Saldo consultado (fetch_balance) | total de ativos=%d",
    "Binance time offset synced (%s): %sms": "Diferença de horário Binance sincronizada | contexto=%s diferença=%sms",
    "Unable to sync time offset (%s): %s": "Não foi possível sincronizar o horário Binance | contexto=%s erro=%s",
    "create_market_order - symbol=%s side=%s qty=%s": "Solicitação de ordem a mercado | symbol=%s side=%s qty=%s",
    "Market order placed - id=%s status=%s": "Ordem a mercado aceita pela Binance | order_id=%s status=%s",
    "create_limit_order - symbol=%s side=%s qty=%s price=%s": "Solicitação de ordem limitada | symbol=%s side=%s quantity=%s price=%s",
    "Limit order placed - id=%s status=%s": "Ordem limitada aceita pela Binance | order_id=%s status=%s",
    "cancel_order - id=%s symbol=%s": "Solicitação de cancelamento | order_id=%s symbol=%s",
    "Order cancelled - id=%s": "Ordem cancelada | order_id=%s",
    "fetch_open_orders - found %d open orders.": "Ordens abertas consultadas (fetch_open_orders) | quantidade=%d",
    "BinanceMarketDataClient connected in LIVE PUBLIC mode.": "Cliente público Binance de dados de mercado conectado em modo LIVE.",
    "BinanceMarketDataClient disconnected.": "Cliente público Binance de dados de mercado desconectado.",
    "fetch_ticker (live public) - symbol=%s last=%s": "Cotação pública consultada | symbol=%s last=%s",
    "download_historical - symbol=%s tf=%s start=%s end=%s": "Início da coleta histórica | symbol=%s timeframe=%s início=%s fim=%s",
    "download_historical returned no data.": "A coleta histórica não retornou dados.",
    "download_historical complete - candles=%d new_rows=%d": "Coleta histórica concluída | candles=%d novas_linhas=%d",
    "get_latest_candles - symbol=%s tf=%s limit=%d": "Consulta de candles recentes | symbol=%s timeframe=%s limite=%d",
    "_paginate - empty batch, stopping.": "Paginação encerrada: lote vazio.",
    "market-data-daemon cycle=%d contexts=%d": "Daemon de dados de mercado | ciclo=%d contextos=%d",
    "market-data-daemon interrupted by user": "Daemon de dados de mercado interrompido pelo usuário.",
    "market-data-daemon download exhausted retries symbol=%s tf=%s": "Coleta esgotou as tentativas | symbol=%s timeframe=%s",
    "market-data-daemon retry symbol=%s tf=%s attempt=%d/%d delay=%.2fs error=%s": "Nova tentativa de coleta | symbol=%s timeframe=%s tentativa=%d/%d espera=%.2fs erro=%s",
    "market-data-daemon reconnect: disconnect failed": "Falha ao desconectar durante reconexão do daemon de dados de mercado.",
    "market-data-daemon reconnect: connect failed": "Falha ao conectar durante reconexão do daemon de dados de mercado.",
    "RiskManager input - portfolio=%.2f entry=%.6f stop=%s take=%s risk_pct=%.4f score=%.4f": "Avaliação de entrada pelo RiskManager | portfolio=%.2f entry=%.6f stop=%s take=%s risk_pct=%.4f score=%.4f",
    "RiskManager sizing base - capital=%.2f max_risk_value=%.2f stop_distance=%.6f trailing_stop_pct=%.4f": "Base do dimensionamento RiskManager | capital=%.2f max_risk_value=%.2f stop_distance=%.6f trailing_stop_pct=%.4f",
    "RiskManager quantity from risk model - qty=%.8f": "Quantidade calculada pelo modelo de risco | qty=%.8f",
    "RiskManager quantity after score factor - qty=%.8f stake=%.2f": "Quantidade após fator de confiança | qty=%.8f stake=%.2f",
    "Stake amount %.2f exceeds max stake %.2f - capping quantity.": "Stake %.2f excede o limite de %.2f; quantidade limitada.",
    "RiskManager stake limit validation - max_stake=%.2f qty_after_cap=%.8f stake_after_cap=%.2f capped=%s": "Validação do limite de stake | max_stake=%.2f qty_after_cap=%.8f stake_after_cap=%.2f capped=%s",
    "RiskManager.evaluate_trade - entry=%.4f sl=%.4f tp=%.4f qty=%.6f stake=%.2f rr=%.2f risk_pct=%.4f": "Parâmetros de risco aprovados | entry=%.4f sl=%.4f tp=%.4f qty=%.6f stake=%.2f rr=%.2f risk_pct=%.4f",
    "Trailing stop triggered - entry=%.4f high=%.4f level=%.4f current=%.4f": "Trailing stop acionado | entry=%.4f high=%.4f level=%.4f current=%.4f",
    "fixed_fractional - portfolio=%.2f stake_pct=%.4f price=%.4f stake=%.2f qty=%.6f": "Dimensionamento por fração fixa | portfolio=%.2f stake_pct=%.4f price=%.4f stake=%.2f qty=%.6f",
    "PositionSizer.risk_based details - portfolio=%.2f risk_pct=%.4f entry=%.6f stop=%.6f risk_per_unit=%.6f max_loss=%.2f qty=%.8f": "Detalhes do dimensionamento por risco | portfolio=%.2f risk_pct=%.4f entry=%.6f stop=%.6f risk_per_unit=%.6f max_loss=%.2f qty=%.8f",
    "risk_based - portfolio=%.2f risk_pct=%.4f entry=%.4f sl=%.4f risk_per_unit=%.4f max_loss=%.2f qty=%.6f": "Dimensionamento por risco | portfolio=%.2f risk_pct=%.4f entry=%.4f sl=%.4f risk_per_unit=%.4f max_loss=%.2f qty=%.6f",
    "%s - initialized.": "%s inicializada.",
    "%s - initialized with trend EMA %d.": "%s inicializada com EMA de tendência %d.",
    "%s — initialized.": "%s inicializada.",
    "%s — calculate: computing indicators for %d bars": "%s — cálculo: processando indicadores para %d candles",
    "%s — EMA done (%.3fs)": "%s — EMA concluída (%.3fs)",
    "%s — RSI done (%.3fs)": "%s — RSI concluído (%.3fs)",
    "%s — ATR done (%.3fs)": "%s — ATR concluído (%.3fs)",
    "%s — Bollinger done (%.3fs)": "%s — Bandas de Bollinger concluídas (%.3fs)",
    "%s — ATR buckets done (%.3fs)": "%s — faixas de ATR concluídas (%.3fs)",
    "%s — Bollinger positions done (%.3fs)": "%s — posições nas bandas de Bollinger concluídas (%.3fs)",
    "%s — indicators pre-computed: %d bars in %.2fs": "%s — indicadores pré-calculados: %d candles em %.2fs",
    "%s — loaded H27 rule model from %s": "%s — modelo de regras H27 carregado de %s",
    "%s – BUY breakout price=%.4f breakout=%.4f atr=%.4f stop=%.4f rr=%.2f score=%.2f": "%s – sinal COMPRA por rompimento | price=%.4f breakout=%.4f atr=%.4f stop=%.4f rr=%.2f score=%.2f",
    "%s - BUY signal price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f": "%s - sinal COMPRA | price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f",
    "%s - BUY signal price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f": "%s - sinal COMPRA | price=%.4f atr=%.4f stop=%.4f risk=%.4f rr=%.2f reward=%.4f take=%.4f score=%.2f",
    "%s - SELL signal reason=%s price=%.4f pnl=%.2f%%": "%s - sinal VENDA | reason=%s price=%.4f pnl=%.2f%%",
    "Optimization aborted: %s": "Otimização interrompida: %s",
    "Downloaded %d candles for %s/%s.": "Candles baixados: %d para %s/%s.",
    "No data in database for %s/%s. Run 'download' first.": "Não há dados no banco para %s/%s. Execute 'download' primeiro.",
    "No data for %s/%s. Run 'download' first.": "Não há dados para %s/%s. Execute 'download' primeiro.",
    "Paper trading complete - final=%.2f initial=%.2f PnL=%.2f (%.2f%%)": "Paper trading concluído | final=%.2f inicial=%.2f PnL=%.2f (%.2f%%)",
    "Exchange: %s": "Exchange configurada: %s",
    "Strategy: %s": "Estratégia: %s",
    "Strategy Version: %s": "Versão da estratégia: %s",
    "Timeframe: %s": "Período: %s",
    "Mode: LIVE": "Modo: LIVE",
}

_PT_PREFIXES = (
    ("LIVE service failed during initialization or execution.", "Falha no serviço LIVE durante inicialização ou execução."),
    ("LIVE service failed", "Falha no serviço LIVE"),
    ("LivePositionStore.save_all ->", "Estado LIVE salvo"),
    ("LivePositionStore.load_all -> loaded legacy single state", "Estado LIVE legado de posição única carregado"),
    ("LivePositionStore.load_all -> loaded", "Estados LIVE carregados"),
    ("LivePositionStore.clear ->", "Estado LIVE removido"),
    ("LIVE recovery blocked:", "Recuperação LIVE bloqueada:"),
    ("BUY blocked:", "COMPRA bloqueada:"),
    ("SELL_BLOCKED", "VENDA_BLOQUEADA | SELL_BLOCKED"),
    ("_db_create_trade failed:", "Falha ao criar trade no banco:"),
    ("_db_update_trade_after_buy failed:", "Falha ao atualizar trade após compra no banco:"),
    ("_db_cancel_trade failed:", "Falha ao cancelar trade no banco:"),
    ("_db_close_trade failed:", "Falha ao fechar trade no banco:"),
    ("_db_update_open_trade_quantity failed:", "Falha ao atualizar quantidade OPEN no banco:"),
    ("Maximum open positions reached.", "Limite máximo de posições OPEN atingido."),
    ("Context already has an open position. Skipping BUY.", "O contexto já possui posição OPEN; compra ignorada."),
    ("BUY accepted |", "Compra aceita (BUY accepted) |"),
    ("SELL executed |", "Venda confirmada (SELL executed) |"),
    ("Open positions:", "Posições OPEN:"),
    ("Capital committed:", "Capital comprometido:"),
    ("Free capital:", "Capital livre:"),
    ("Capital free:", "Capital livre:"),
    ("Position closed", "Posição encerrada"),
    ("Position opened", "Posição aberta"),
    ("Trailing stop", "Trailing stop"),
    ("Order persisted -", "Ordem persistida |"),
    ("Execution failed:", "Falha na execução:"),
    ("Failed to", "Falha ao"),
    ("Unable to", "Não foi possível"),
    ("Insufficient available portfolio value", "Saldo disponível insuficiente para a operação"),
    ("Insufficient free USDT", "Saldo livre em USDT insuficiente"),
    ("InsufficientFunds", "InsufficientFunds"),
    ("InvalidOrder", "InvalidOrder"),
    ("NetworkError", "NetworkError"),
    ("Optimization aborted:", "Otimização interrompida:"),
    ("Paper trading complete -", "Paper trading concluído |"),
    ("BUY signal", "sinal de COMPRA"),
    ("SELL signal", "sinal de VENDA"),
    (" initialized with trend EMA ", " inicializada com EMA de tendência "),
    (" initialized.", " inicializada."),
    (" indicators pre-computed:", " indicadores pré-calculados:"),
    ("— initialized.", "— inicializada."),
    ("— calculate: computing indicators for", "— cálculo: processando indicadores para"),
    ("— EMA done", "— EMA concluída"),
    ("— RSI done", "— RSI concluído"),
    ("— ATR done", "— ATR concluído"),
    ("— Bollinger done", "— bandas de Bollinger concluídas"),
    (" — loaded H27 rule model from ", " — modelo de regras H27 carregado de "),
    ("No data in database", "Não há dados no banco"),
    ("No data for", "Não há dados para"),
)

_RESULT_PNL = re.compile(
    r"(?:^|[ |])(?:realized_pnl_usdt|net_realized|net_pnl|PnL|net)=(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LIVE_STATE_FIELDS = re.compile(
    r"OPEN=(\d+)/(\d+) DUST=(\d+) DUST_qty=([^ ]+) "
    r"capital_committed_usdt=([\d.]+) dust_cost_usdt=([\d.]+) capital_free_usdt=([\d.]+|UNKNOWN)"
)
_LOG_CATEGORIES = (
    ("execution.live_trading_service", "LIVE"),
    ("execution.order_executor", "ORDENS"),
    ("execution.live_risk_service", "RISCO"),
    ("risk.", "RISCO"),
    ("exchange.binance", "BINANCE"),
    ("exchange.data_downloader", "DADOS DE MERCADO"),
    ("market_data.daemon", "DADOS DE MERCADO"),
    ("strategies.", "ESTRATÉGIA"),
    ("monitor_live_trades", "MONITORAMENTO"),
    ("audit_live_readiness", "AUDITORIA"),
)


def _localize_message(message: str) -> str:
    """Traduz templates operacionais e acrescenta leitura humana sem alterar campos estáveis."""
    match = next(
        ((source, target) for source, target in _PT_PREFIXES if message.startswith(source)),
        None,
    )
    if match is not None:
        source, target = match
        message = target + message[len(source):]

    if message.startswith("LIVE_STATE |"):
        fields = _LIVE_STATE_FIELDS.search(message)
        if fields:
            opened, maximum, dust_count, dust_qty, committed, dust_cost, free = fields.groups()
            if free == "UNKNOWN":
                free = "INDISPONIVEL (consulta atual falhou)"
            message += (
                " | leitura: posições OPEN %s/%s; DUST %s (%s); "
                "capital OPEN %s USDT; custo residual DUST %s USDT; USDT livre %s"
                % (opened, maximum, dust_count, dust_qty, committed, dust_cost, free)
            )
            next_action = (
                "avaliar a posição OPEN no próximo candle fechado"
                if int(opened) > 0 else
                "aguardar sinal BUY válido em candle fechado; DUST não ocupa posição ativa"
            )
            message += " | próxima ação: %s" % next_action

    if "resultado=" not in message:
        match = _RESULT_PNL.search(message)
        if match:
            pnl = float(match.group(1))
            outcome = "PREJUÍZO" if pnl < -1e-12 else "LUCRO" if pnl > 1e-12 else "NEUTRO"
            message += " | resultado=%s" % outcome

    return message


def _category_for_logger(logger_name: str) -> str | None:
    for prefix, category in _LOG_CATEGORIES:
        if logger_name == prefix or logger_name.startswith(prefix):
            return category
    return None


def build_formatter() -> logging.Formatter:
    class BrazilianOperationalFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            localized = copy.copy(record)
            raw_template = record.msg
            if isinstance(raw_template, str) and raw_template in _PT_BR_TEMPLATES:
                localized.msg = _PT_BR_TEMPLATES[raw_template]
                localized.args = record.args
            else:
                localized.msg = record.msg
                localized.args = record.args
            localized.msg = _localize_message(localized.getMessage())
            localized.args = ()
            category = _category_for_logger(record.name)
            if category:
                localized.msg = f"[{category}] {localized.msg}"
            return super().format(localized)

    return BrazilianOperationalFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(processName)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
