"""
Classe base abstrata para clientes de exchange.

Decisão de projeto: definir uma interface por meio de ABC garante que
implementações alternativas de exchange (por exemplo, Kraken ou Coinbase)
possam ser substituídas sem alterar o restante da aplicação (Princípio da
Inversão de Dependência).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class BaseExchange(ABC):
    """
    Interface abstrata que todo adaptador de exchange deve implementar.

    Todos os valores de preço/quantidade usam float. Todos os timestamps usam UTC.
    """

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Inicializa a conexão/autentica com a exchange."""

    @abstractmethod
    def disconnect(self) -> None:
        """Encerra as conexões e libera recursos."""

    # ------------------------------------------------------------------
    # Dados de mercado
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """
        Busca dados OHLCV (candles).

        Argumentos:
            symbol: Par de negociação, por exemplo, ``BTC/USDT``.
            timeframe: Intervalo dos candles, por exemplo, ``1h``.
            since: Horário inicial como timestamp Unix em milissegundos.
            limit: Número máximo de candles a retornar.

        Retorno:
            DataFrame com as colunas [open, high, low, close, volume],
            indexado por um DatetimeIndex com fuso horário UTC.
        """

    @abstractmethod
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """
        Retorna as informações mais recentes do ticker de *symbol*.

        O dict retornado deve incluir pelo menos: ``last``, ``bid``, ``ask``,
        ``volume`` e ``timestamp``.
        """

    @abstractmethod
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Retorna o livro de ofertas atual de *symbol*."""

    # ------------------------------------------------------------------
    # Conta
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_balance(self) -> dict[str, Any]:
        """Retorna os saldos da conta indexados por moeda."""

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    @abstractmethod
    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """
        Envia uma ordem a mercado.

        Argumentos:
            symbol: Par de negociação.
            side: ``buy`` ou ``sell``.
            quantity: Quantidade na moeda base.

        Retorno:
            Dict de resposta da ordem da exchange.
        """

    @abstractmethod
    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict[str, Any]:
        """Envia uma ordem limitada."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancela uma ordem aberta pelo ID atribuído pela exchange."""

    @abstractmethod
    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Busca o estado atual de uma ordem específica."""

    @abstractmethod
    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Retorna todas as ordens atualmente abertas, opcionalmente filtradas por ativo."""
