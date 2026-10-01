"""
Adaptador da exchange Binance baseado em ccxt.

Decisão de projeto: ccxt é usada como biblioteca principal porque normaliza
respostas de mais de 100 exchanges. python-binance é mantida como dependência
opcional para streams WebSocket específicos da Binance que não são cobertos
por ccxt.
"""
from __future__ import annotations

import os
from typing import Any

import ccxt
import pandas as pd

from config.settings import settings
from exchange.base_exchange import BaseExchange
from utils.helpers import normalize_ohlcv_dataframe, retry, timeit, timestamp_to_datetime
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger(__name__)

# As ordens reais permanecem bloqueadas, a menos que a CLI live seja iniciada com --enable-real-orders.
_REAL_ORDERS_ARMED = False


def arm_real_orders() -> None:
    global _REAL_ORDERS_ARMED
    _REAL_ORDERS_ARMED = True


def real_orders_armed() -> bool:
    return _REAL_ORDERS_ARMED


class BinanceClient(BaseExchange):
    """
    Adaptador da exchange Binance.

    Usa ccxt internamente, com suporte à testnet por meio da flag ``sandbox``.
    Todos os métodos públicos registram início, término e erros, garantindo
    a auditabilidade de cada interação com a API.
    """

    def __init__(self) -> None:
        self._exchange: ccxt.binance | None = None

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Inicializa a instância ccxt da Binance e valida as credenciais."""
        cfg = settings.binance
        recv_window_ms = max(5000, int(os.getenv("BINANCE_RECV_WINDOW_MS", "60000")))
        self._exchange = ccxt.binance(
            {
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                    "recvWindow": recv_window_ms,
                },
            }
        )

        if cfg.testnet:
            self._exchange.set_sandbox_mode(True)
            logger.info("BinanceClient connected in TESTNET mode.")
        else:
            logger.info("BinanceClient connected in LIVE mode.")

        # Carrega os mercados antecipadamente para que as chamadas seguintes não gerem solicitações extras
        self._exchange.load_markets()
        self._sync_time_offset(context="connect")
        logger.debug("Markets loaded - %d symbols available.", len(self._exchange.markets))

    def disconnect(self) -> None:
        """Libera quaisquer sessões abertas."""
        if self._exchange:
            # O ccxt nao mantem conexoes persistentes, mas chamar close
            # e uma boa pratica para garantir compatibilidade futura.
            logger.info("BinanceClient disconnected.")
            self._exchange = None

    @property
    def _client(self) -> ccxt.binance:
        """Retorna o cliente ccxt subjacente ou gera uma exceção se não estiver conectado."""
        if self._exchange is None:
            raise RuntimeError(
                "BinanceClient is not connected. Call connect() first."
            )
        return self._exchange

    def is_symbol_supported(self, symbol: str) -> bool:
        """Retorna True quando o ativo existe nos mercados carregados da Binance."""
        symbol = validate_symbol(symbol)
        return symbol in self._client.markets

    # ------------------------------------------------------------------
    # Dados de mercado
    # ------------------------------------------------------------------

    @timeit
    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """
        Busca dados OHLCV e retorna um DataFrame normalizado.

        Argumentos:
            symbol: Par de negociação (por exemplo, ``BTC/USDT``).
            timeframe: Intervalo dos candles (por exemplo, ``1h``).
            since: Horário inicial em milissegundos UTC.
            limit: Número máximo de candles a retornar (máximo da Binance = 1000).

        Retorno:
            DataFrame OHLCV normalizado, indexado por DatetimeIndex em UTC.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)

        logger.info(
            "fetch_ohlcv - symbol=%s timeframe=%s since=%s limit=%s",
            symbol,
            timeframe,
            since,
            limit,
        )

        raw = self._client.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit or 500,
        )

        if not raw:
            logger.warning("fetch_ohlcv returned empty data for %s/%s.", symbol, timeframe)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.drop(columns=["timestamp"], inplace=True)

        return normalize_ohlcv_dataframe(df)

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Retorna o ticker mais recente de *symbol*."""
        symbol = validate_symbol(symbol)
        ticker = self._client.fetch_ticker(symbol)
        logger.debug("fetch_ticker - symbol=%s last=%s", symbol, ticker.get("last"))
        return ticker

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Retorna o livro de ofertas de *symbol*."""
        symbol = validate_symbol(symbol)
        return self._client.fetch_order_book(symbol, limit=limit)

    # ------------------------------------------------------------------
    # Conta
    # ------------------------------------------------------------------

    @retry(max_attempts=3, delay_seconds=2.0)
    def fetch_balance(self) -> dict[str, Any]:
        """Retorna o saldo da conta."""
        try:
            balance = self._client.fetch_balance()
        except Exception as exc:
            if self._is_timestamp_window_error(exc):
                logger.warning(
                    "Binance returned timestamp/recvWindow error on fetch_balance; syncing clock and retrying once."
                )
                self._sync_time_offset(context="fetch_balance")
                balance = self._client.fetch_balance()
            else:
                raise
        logger.debug("fetch_balance - total currencies: %d", len(balance.get("total", {})))
        return balance

    def _sync_time_offset(self, context: str) -> None:
        """Sincroniza no ccxt a diferença entre o relógio local e o do servidor, quando disponível."""
        try:
            offset_ms = self._client.load_time_difference()
            logger.info("Binance time offset synced (%s): %sms", context, offset_ms)
        except Exception as exc:
            logger.warning("Unable to sync Binance time offset (%s): %s", context, exc)

    @staticmethod
    def _is_timestamp_window_error(exc: Exception) -> bool:
        message = str(exc)
        return "-1021" in message or "outside of the recvWindow" in message

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """Envia uma ordem a mercado."""
        self._guard_live_trading()
        symbol = validate_symbol(symbol)
        logger.info(
            "create_market_order - symbol=%s side=%s qty=%s", symbol, side, quantity
        )
        order = self._client.create_order(symbol, "market", side, quantity)
        logger.info("Market order placed - id=%s status=%s", order["id"], order["status"])
        return order

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        """Envia uma ordem limitada."""
        self._guard_live_trading()
        symbol = validate_symbol(symbol)
        logger.info(
            "create_limit_order - symbol=%s side=%s qty=%s price=%s",
            symbol,
            side,
            quantity,
            price,
        )
        order = self._client.create_order(symbol, "limit", side, quantity, price)
        logger.info("Limit order placed - id=%s status=%s", order["id"], order["status"])
        return order

    @retry(max_attempts=3, delay_seconds=1.0)
    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancela uma ordem aberta."""
        symbol = validate_symbol(symbol)
        logger.info("cancel_order - id=%s symbol=%s", order_id, symbol)
        result = self._client.cancel_order(order_id, symbol)
        logger.info("Order cancelled - id=%s", order_id)
        return result

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Busca o estado atual de uma ordem específica."""
        symbol = validate_symbol(symbol)
        return self._client.fetch_order(order_id, symbol)

    @retry(max_attempts=3, delay_seconds=1.0)
    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Retorna ordens abertas, opcionalmente filtradas por ativo."""
        if symbol:
            symbol = validate_symbol(symbol)
        orders = self._client.fetch_open_orders(symbol)
        logger.debug("fetch_open_orders - found %d open orders.", len(orders))
        return orders

    def fetch_symbol_trading_filters(self, symbol: str) -> dict[str, float]:
        """Retorna os filtros oficiais de negociação da Binance para um ativo."""
        symbol = validate_symbol(symbol)
        market = self._client.market(symbol)

        limits = market.get("limits", {}) or {}
        amount_limits = limits.get("amount", {}) or {}
        cost_limits = limits.get("cost", {}) or {}

        min_qty = float(amount_limits.get("min") or 0.0)
        min_notional = float(cost_limits.get("min") or 0.0)

        info = market.get("info", {}) or {}
        filters = info.get("filters", []) or []
        step_size = 0.0
        for item in filters:
            if item.get("filterType") == "LOT_SIZE":
                step_size = float(item.get("stepSize") or 0.0)
                if min_qty <= 0.0:
                    min_qty = float(item.get("minQty") or 0.0)
            if item.get("filterType") == "NOTIONAL" and min_notional <= 0.0:
                min_notional = float(item.get("minNotional") or 0.0)
            if item.get("filterType") == "MIN_NOTIONAL" and min_notional <= 0.0:
                min_notional = float(item.get("minNotional") or 0.0)

        if step_size <= 0.0:
            precision = market.get("precision", {}) or {}
            amount_precision = precision.get("amount")
            if amount_precision:
                step_size = float(amount_precision)

        if min_notional <= 0.0 or min_qty <= 0.0 or step_size <= 0.0:
            raise RuntimeError(
                "Unable to resolve Binance trading filters "
                f"for {symbol}: min_notional={min_notional} min_qty={min_qty} step_size={step_size}"
            )

        return {
            "min_notional": min_notional,
            "min_qty": min_qty,
            "step_size": step_size,
        }

    # ------------------------------------------------------------------
    # Protecoes
    # ------------------------------------------------------------------

    def _guard_live_trading(self) -> None:
        """
        Gera RuntimeError quando o modo paper trading está ativo.

        Isso evita o envio acidental de ordens reais durante os testes.
        """
        if settings.is_paper_trading:
            raise RuntimeError(
                "Live order rejected: paper trading mode is active. "
                "Set PAPER_TRADING=false in .env to enable real orders."
            )
        if not _REAL_ORDERS_ARMED:
            raise RuntimeError(
                "Live order rejected: real orders are not armed. "
                "Start 'main.py live' with --enable-real-orders to allow them."
            )
