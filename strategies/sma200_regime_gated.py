"""Registered Paper-Live exposure of the frozen SMA200 regime-gated strategy."""
from __future__ import annotations

from research.external_strategy_replication_strategies import Sma200RegimeGatedStrategy
from strategies.registry import register_strategy

register_strategy(
    name="Sma200RegimeGated",
    version="v1",
    family="trend",
    description="Frozen SMA200/ADX causal regime gate: TRENDING_BULL long, otherwise cash.",
    parameters=["sma_period", "adx_period", "adx_threshold", "slope_lookback"],
    indicators=["SMA200", "ADX14", "SMA200Slope20"],
    categories=["trend", "regime_gated", "spot", "paper"],
    compatibility=["paper_trading", "database", "checkpoints", "resume"],
    aliases=["sma200_regime_gated", "SMA200RegimeGatedBNB4H"],
)(Sma200RegimeGatedStrategy)
