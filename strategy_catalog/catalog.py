from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class StrategyCatalogEntry:
    name: str
    description: str
    origin: str
    reference: str
    family: str
    indicators: tuple[str, ...]
    recommended_markets: tuple[str, ...]
    default_parameters: dict[str, float | int]
    included_at: date
    lifecycle_status: str = "Implementada"


class StrategyCatalog:
    """Permanent catalog with public classic strategies and metadata."""

    def __init__(self) -> None:
        today = date.today()
        self._entries: list[StrategyCatalogEntry] = [
            StrategyCatalogEntry(
                name="ClassicEMACrossover",
                description="Trend following via fast/slow EMA crossover.",
                origin="Livro",
                reference="Murphy (1999), Technical Analysis of the Financial Markets",
                family="Tendência",
                indicators=("EMA",),
                recommended_markets=("Crypto", "FX", "Equities"),
                default_parameters={"ema_fast": 12, "ema_slow": 26},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicSMACrossover",
                description="Trend following via fast/slow SMA crossover.",
                origin="Livro",
                reference="Pring (2002), Technical Analysis Explained",
                family="Tendência",
                indicators=("SMA",),
                recommended_markets=("Crypto", "Equities", "Futures"),
                default_parameters={"sma_fast": 10, "sma_slow": 30},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicMACDTrend",
                description="Momentum trend confirmation using MACD line and signal line.",
                origin="Livro",
                reference="Appel (1979), The Moving Average Convergence-Divergence Trading Method",
                family="Momentum",
                indicators=("MACD",),
                recommended_markets=("Crypto", "Equities", "FX"),
                default_parameters={"macd_fast": 12, "macd_slow": 26, "macd_signal": 9},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicRSIMeanReversion",
                description="Mean reversion entries in oversold RSI regimes.",
                origin="Livro",
                reference="Wilder (1978), New Concepts in Technical Trading Systems",
                family="Reversão",
                indicators=("RSI",),
                recommended_markets=("Crypto", "Equities", "FX"),
                default_parameters={"rsi_period": 14, "rsi_buy": 30},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicBollingerReversal",
                description="Reversion from Bollinger lower band extremes.",
                origin="Livro",
                reference="Bollinger (2001), Bollinger on Bollinger Bands",
                family="Reversão",
                indicators=("BollingerBands",),
                recommended_markets=("Crypto", "Equities"),
                default_parameters={"bb_period": 20, "bb_std": 2.0},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicDonchianBreakout",
                description="Channel breakout strategy inspired by Turtle rules.",
                origin="Livro",
                reference="Donchian channels, Turtle Trading rules",
                family="Breakout",
                indicators=("Donchian",),
                recommended_markets=("Futures", "Crypto", "FX"),
                default_parameters={"donchian_window": 20},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicATRBreakout",
                description="Volatility breakout based on ATR expansion.",
                origin="Livro",
                reference="Wilder ATR applications, public quant implementations",
                family="Volatilidade",
                indicators=("ATR",),
                recommended_markets=("Crypto", "Futures"),
                default_parameters={"atr_period": 14, "atr_mult": 1.5},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicVWAPReversion",
                description="Reversion toward VWAP after negative dislocation.",
                origin="Open Source",
                reference="QuantConnect/Quantpedia style VWAP mean reversion concepts",
                family="Reversão",
                indicators=("VWAP",),
                recommended_markets=("Crypto", "Equities"),
                default_parameters={"vwap_dev_pct": 0.3},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicKeltnerChannel",
                description="Trend continuation/pullback inside Keltner envelope.",
                origin="Livro",
                reference="Keltner Channel public formulations",
                family="Volatilidade",
                indicators=("EMA", "ATR"),
                recommended_markets=("Crypto", "FX", "Equities"),
                default_parameters={"ema_period": 20, "atr_period": 14, "kc_mult": 2.0},
                included_at=today,
            ),
            StrategyCatalogEntry(
                name="ClassicDualMomentum",
                description="Dual momentum with trend filter (return momentum + EMA trend).",
                origin="Livro",
                reference="Antonacci (2014), Dual Momentum Investing",
                family="Momentum",
                indicators=("ROC", "EMA"),
                recommended_markets=("Crypto", "Equities", "ETF"),
                default_parameters={"momentum_window": 20, "ema_trend": 50},
                included_at=today,
            ),
        ]

    def entries(self) -> list[StrategyCatalogEntry]:
        return list(self._entries)

    def strategy_names(self) -> list[str]:
        return [entry.name for entry in self._entries]
