"""
Classe base abstrata de todas as estratégias de negociação.

Decisão de projeto: impor uma interface fixa (initialize / calculate /
entry_signal / exit_signal / score) permite que todas as estratégias sejam
intercambiáveis no mecanismo de backtest e no trader paper/live, sem lógica
condicional no código chamador.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd


class SignalType(str, Enum):
    """Valores de sinal que uma estratégia pode emitir."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategySignal:
    """
    Encapsula a saída de uma estratégia para uma única avaliação.

    Atributos:
        signal: BUY, SELL ou HOLD.
        price: Preço de referência no momento em que o sinal é gerado.
        timestamp: Horário UTC do sinal.
        score: Valor numérico de confiança/força no intervalo [0, 1].
        stop_loss: Preço sugerido de stop-loss (absoluto).
        take_profit: Preço sugerido de take-profit (absoluto).
        trailing_stop_pct: Stop móvel opcional como fração do preço.
        metadata: Dados adicionais arbitrários para registro/depuração.
    """

    signal: SignalType
    price: float
    timestamp: datetime
    score: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Interface que todas as implementações de estratégia devem satisfazer.

    Lifecycle::

        strategy.initialize()
        for each candle batch:
            strategy.calculate(df)
            signal = strategy.entry_signal(df)
            if open trade:
                signal = strategy.exit_signal(df, entry_price)

    As subclasses definem seus próprios parâmetros de indicadores em ``__init__``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Identificador exclusivo da estratégia usado nos logs e nos registros do banco de dados."""

    @abstractmethod
    def initialize(self) -> None:
        """
        Configuração única: instancia os indicadores e carrega qualquer estado necessário.

        Chamado uma vez antes de a estratégia começar a processar dados.
        """

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Enriquece *df* com todas as colunas de indicadores exigidas por esta estratégia.

        Argumentos:
            df: DataFrame OHLCV bruto.

        Retorna:
            Novo DataFrame (uma cópia de *df*) com as colunas de indicadores adicionadas.
            Não deve modificar a entrada.
        """

    @abstractmethod
    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        """
        Avalia se deve abrir uma nova posição com base em *df* enriquecido.

        Argumentos:
            df: Saída de ``calculate()``.

        Retorna:
            StrategySignal com BUY, SELL (para posição vendida) ou HOLD.
        """

    @abstractmethod
    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        """
        Avalia se deve fechar uma posição existente.

        Argumentos:
            df: Saída de ``calculate()``.
            entry_price: Preço pelo qual a posição foi aberta.

        Retorna:
            StrategySignal com SELL (fechar posição comprada), BUY (fechar posição vendida) ou HOLD.
        """

    @abstractmethod
    def score(self, df: pd.DataFrame) -> float:
        """
        Retorna uma pontuação de confiança no intervalo [0, 1] para a configuração atual.

        Valores maiores indicam maior convicção. As pontuações são usadas para
        dimensionar posições e filtrar operações.

        Argumentos:
            df: Saída de ``calculate()``.

        Retorna:
            Número de ponto flutuante no intervalo [0, 1].
        """

    def prepare_dataset(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Pré-calcula e armazena em cache todas as características exigidas por esta estratégia para *df*.

        A implementação padrão calcula o conjunto de dados enriquecido uma vez e o reutiliza
        enquanto o conjunto de dados e os parâmetros da estratégia não forem alterados.
        """

        cache_key = self._build_dataset_cache_key(df, symbol=symbol, timeframe=timeframe)
        cached_key = getattr(self, "_prepared_dataset_cache_key", None)
        cached_df = getattr(self, "_prepared_dataset_cache", None)

        if cached_key == cache_key and cached_df is not None:
            return cached_df

        prepared = self.calculate(df)
        self._prepared_dataset_cache_key = cache_key
        self._prepared_dataset_cache = prepared
        self._prepared_dataset_aux_cache = {}
        return prepared

    def invalidate_prepared_dataset(self) -> None:
        """Limpa o conjunto de dados preparado em cache e quaisquer caches auxiliares de execução."""

        self._prepared_dataset_cache_key = None
        self._prepared_dataset_cache = None
        self._prepared_dataset_aux_cache = {}

    def cache_payload(self, name: str, payload: Any) -> None:
        """Armazena uma carga útil de execução reutilizável vinculada ao cache atual do conjunto de dados."""

        aux = getattr(self, "_prepared_dataset_aux_cache", None)
        if aux is None:
            aux = {}
            self._prepared_dataset_aux_cache = aux
        aux[str(name)] = payload

    def cached_payload(self, name: str, default: Any = None) -> Any:
        """Lê uma carga útil de execução reutilizável vinculada ao cache atual do conjunto de dados."""

        aux = getattr(self, "_prepared_dataset_aux_cache", None)
        if aux is None:
            return default
        return aux.get(str(name), default)

    def execution_cache_signature(self) -> tuple[tuple[str, Any], ...]:
        """
        Retorna uma assinatura estável dos parâmetros escalares da estratégia para invalidar o cache.
        """

        signature: list[tuple[str, Any]] = []
        for key, value in sorted(self.__dict__.items()):
            if "cache" in key:
                continue
            if isinstance(value, (int, float, str, bool, type(None))):
                signature.append((key, value))
            elif isinstance(value, Path):
                signature.append((key, str(value)))
        return tuple(signature)

    def _build_dataset_cache_key(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None,
        timeframe: str | None,
    ) -> tuple[Any, ...]:
        first_idx = None if df.empty else str(df.index[0])
        last_idx = None if df.empty else str(df.index[-1])
        return (
            self.__class__.__name__,
            symbol,
            timeframe,
            len(df),
            tuple(str(col) for col in df.columns),
            first_idx,
            last_idx,
            self.execution_cache_signature(),
        )
