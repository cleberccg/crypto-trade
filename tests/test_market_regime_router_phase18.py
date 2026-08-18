from __future__ import annotations

import pandas as pd

from main import _parse_args
from research.services.market_regime_router_phase18 import (
    build_router_map,
    classify_market_regimes,
    evaluate_hypothesis,
)


def _sample_candles() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=120, freq="5min", tz="UTC")
    prices = []
    current = 100.0
    for i in range(120):
        if i < 40:
            current += 0.7
        elif i < 80:
            current -= 0.6
        else:
            current += 0.03

        vol_scale = 4.5 if i % 13 == 0 else 1.0
        prices.append(
            {
                "open": current - 0.2,
                "high": current + (1.3 * vol_scale),
                "low": current - (1.1 * vol_scale),
                "close": current,
                "volume": 100 + i,
            }
        )

    return pd.DataFrame(prices, index=index)


def test_classify_market_regimes_has_phase18_dimensions() -> None:
    classified = classify_market_regimes(_sample_candles())

    assert "trend_bucket" in classified.columns
    assert "vol_regime" in classified.columns
    assert "regime_key" in classified.columns
    assert set(classified["trend_bucket"].dropna().unique()).issubset({"bullish", "bearish", "sideways"})
    assert set(classified["vol_regime"].dropna().unique()).issubset(
        {"high_volatility", "normal_volatility", "low_volatility"}
    )


def test_build_router_map_prefers_higher_context_score() -> None:
    rows = [
        {
            "strategy": "A",
            "platform_strategy_name": "APlatform",
            "symbol": "BTC/USDT",
            "timeframe": "5m",
            "trend_bucket": "bullish",
            "vol_regime": "high_volatility",
            "context_score": 71.0,
            "number_of_trades": 23,
        },
        {
            "strategy": "B",
            "platform_strategy_name": "BPlatform",
            "symbol": "BTC/USDT",
            "timeframe": "5m",
            "trend_bucket": "bullish",
            "vol_regime": "high_volatility",
            "context_score": 81.5,
            "number_of_trades": 17,
        },
    ]

    mapping = build_router_map(rows)
    assert len(mapping) == 1
    assert mapping[0]["recommended_strategy"] == "B"
    assert mapping[0]["recommended_platform_strategy"] == "BPlatform"


def test_evaluate_hypothesis_confirms_b_when_router_is_superior() -> None:
    single = {
        "profit_factor": 1.10,
        "sharpe": 0.55,
        "expectancy": 0.02,
        "drawdown_pct": 0.18,
        "return_pct": 0.14,
    }
    router = {
        "profit_factor": 1.35,
        "sharpe": 0.91,
        "expectancy": 0.05,
        "drawdown_pct": 0.13,
        "return_pct": 0.20,
    }
    robustness_rows = [
        {"router_better": True},
        {"router_better": True},
        {"router_better": True},
        {"router_better": False},
    ]

    decision = evaluate_hypothesis(single, router, robustness_rows)
    assert decision["hypothesis_with_more_evidence"] == "B"
    assert decision["conclusion"] == "confirmada"


def test_cli_parser_market_regime_router(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "market-regime-router",
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--timeframes",
            "5m,1h",
        ],
    )
    args = _parse_args()
    assert args.command == "market-regime-router"
    assert args.symbols == "BTC/USDT,ETH/USDT"
    assert args.timeframes == "5m,1h"
