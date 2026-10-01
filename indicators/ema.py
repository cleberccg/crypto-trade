"""
Indicador de média móvel exponencial (Exponential Moving Average, EMA).
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class EMA(BaseIndicator):
    """
    Média móvel exponencial.

    Argumentos:
        period: Número de períodos do cálculo da EMA.
    """

    def __init__(self, period: int = 20) -> None:
        if period < 1:
            raise ValueError(f"EMA period must be >= 1, got {period}.")
        self._period = period

    @property
    def name(self) -> str:
        return f"ema_{self._period}"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calcula a EMA na série de preços de fechamento.

        Argumentos:
            df: DataFrame OHLCV.

        Retorno:
            Series chamada ``ema_<period>`` com os valores da EMA.
        """
        self._validate_min_length(df, self._period)
        series = df["close"].ewm(span=self._period, adjust=False).mean()
        series.name = self.name
        return series
