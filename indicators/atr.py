"""
Indicador de amplitude verdadeira média (Average True Range, ATR).
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class ATR(BaseIndicator):
    """
    Amplitude verdadeira média (Average True Range): mede a volatilidade do mercado.

    Usado com frequência para:
    - Definir distâncias dinâmicas de stop-loss.
    - Dimensionar posições proporcionalmente à volatilidade atual.

    Argumentos:
        period: Período de suavização (padrão 14).
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError(f"ATR period must be >= 1, got {period}.")
        self._period = period

    @property
    def name(self) -> str:
        return f"atr_{self._period}"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calcula o ATR usando o método de suavização de Wilder.

        True Range = max(high - low, |high - prev_close|, |low - prev_close|)

        Argumentos:
            df: DataFrame OHLCV (deve conter high, low e close).

        Retorno:
            Series chamada ``atr_<period>`` com os valores de ATR.
        """
        self._validate_min_length(df, self._period + 1)

        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.ewm(com=self._period - 1, adjust=False).mean()
        atr.name = self.name
        return atr
