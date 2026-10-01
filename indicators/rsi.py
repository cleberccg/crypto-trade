"""
Indicador de índice de força relativa (Relative Strength Index, RSI).
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class RSI(BaseIndicator):
    """
    Índice de força relativa (Relative Strength Index) de Wilder.

    Limite de sobrecompra: geralmente 70.
    Limite de sobrevenda: geralmente 30.

    Argumentos:
        period: Período retroativo (padrão 14).
    """

    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError(f"RSI period must be >= 2, got {period}.")
        self._period = period

    @property
    def name(self) -> str:
        return f"rsi_{self._period}"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calcula o RSI usando a suavização de Wilder (equivalente à EMA com
        ``com = period - 1``).

        Argumentos:
            df: DataFrame OHLCV.

        Retorno:
            Series chamada ``rsi_<period>`` com valores no intervalo [0, 100].
        """
        self._validate_min_length(df, self._period + 1)

        delta = df["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)

        avg_gain = gain.ewm(com=self._period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=self._period - 1, adjust=False).mean()

        # Evita divisao por zero: quando avg_loss e 0, RSI = 100
        rs = avg_gain / avg_loss.replace(0.0, float("nan"))
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.fillna(100.0)  # avg_loss era zero -> sem perdas -> RSI = 100

        rsi.name = self.name
        return rsi
