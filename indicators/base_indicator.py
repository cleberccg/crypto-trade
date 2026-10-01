"""
Classe base abstrata para todos os indicadores técnicos.

Decisão de projeto: uma ABC impõe uma interface uniforme para que as
estratégias possam usar qualquer indicador de forma intercambiável. O método
`calculate` recebe um DataFrame e retorna uma nova Series, mantendo os
indicadores livres de efeitos colaterais.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseIndicator(ABC):
    """
        Interface que todas as implementações de indicador devem cumprir.

        Cada subclasse deve:
        - Aceitar configuração por meio de ``__init__``.
        - Implementar ``calculate`` retornando uma ``pd.Series`` nomeada (ou um
            ``pd.DataFrame`` para indicadores com várias saídas, como MACD/Bollinger).
        - Não manter estado entre chamadas (sem estado intermediário armazenado).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome curto e legível usado na identificação das colunas."""

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.Series | pd.DataFrame:
        """
        Calcula o indicador a partir de *df*.

        Argumentos:
            df: DataFrame OHLCV com, no mínimo, as colunas [open, high, low,
                close, volume] e um DatetimeIndex.

        Retorno:
            Uma ``pd.Series`` (saída única) ou um ``pd.DataFrame`` (múltiplas
            saídas), alinhado ao índice de *df*.
        """

    def _validate_min_length(self, df: pd.DataFrame, min_length: int) -> None:
        """
        Gera ValueError se *df* não tiver linhas suficientes para calcular o
        indicador de forma confiável.
        """
        if len(df) < min_length:
            raise ValueError(
                f"{self.name} requires at least {min_length} rows, "
                f"got {len(df)}."
            )
