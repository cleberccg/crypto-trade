"""
Indicador MACD (convergência e divergência de médias móveis; Moving Average Convergence Divergence).
"""
from __future__ import annotations

import pandas as pd

from indicators.base_indicator import BaseIndicator


class MACD(BaseIndicator):
    """
    MACD: indicador de momentum que acompanha tendências.

    Componentes:
    - ``macd``: fast_ema - slow_ema
    - ``signal``: EMA da linha MACD
    - ``histogram``: MACD - signal

    Argumentos:
        fast_period: Período rápido da EMA (padrão 12).
        slow_period: Período lento da EMA (padrão 26).
        signal_period: Período da EMA da linha de sinal (padrão 9).
    """

    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be less than slow_period ({slow_period})."
            )
        self._fast = fast_period
        self._slow = slow_period
        self._signal = signal_period

    @property
    def name(self) -> str:
        return f"macd_{self._fast}_{self._slow}_{self._signal}"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calcula os componentes do MACD.

        Argumentos:
            df: DataFrame OHLCV.

        Retorno:
            DataFrame com as colunas [macd, signal, histogram].
        """
        self._validate_min_length(df, self._slow + self._signal)

        close = df["close"]
        fast_ema = close.ewm(span=self._fast, adjust=False).mean()
        slow_ema = close.ewm(span=self._slow, adjust=False).mean()

        macd_line = fast_ema - slow_ema
        signal_line = macd_line.ewm(span=self._signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return pd.DataFrame(
            {
                "macd": macd_line,
                "signal": signal_line,
                "histogram": histogram,
            },
            index=df.index,
        )
