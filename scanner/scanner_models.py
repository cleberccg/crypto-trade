from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ScannerAsset:
    symbol: str
    liquidity_score: float
    volatility_score: float
    volume_score: float
    spread_score: float
    trend_score: float
    momentum_score: float
    opportunity_score: float
