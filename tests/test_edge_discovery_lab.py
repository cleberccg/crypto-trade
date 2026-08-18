from __future__ import annotations

from pathlib import Path

import pandas as pd

from main import _parse_args
from research.services.edge_discovery_lab import (
    attach_trade_regimes,
    build_scientific_ranking,
    classify_market_regimes,
    metrics_from_trades,
)


def _sample_candles() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=90, freq="5min", tz="UTC")
    prices = []
    current = 100.0
    for i in range(90):
        if i < 30:
            current += 0.8
        elif i < 60:
            current += 0.02
        else:
            current -= 0.9
        vol_scale = 4.0 if i % 11 == 0 else 1.0
        prices.append(
            {
                "open": current - 0.2,
                "high": current + (1.4 * vol_scale),
                "low": current - (1.2 * vol_scale),
                "close": current,
                "volume": 100 + i,
            }
        )
    return pd.DataFrame(prices, index=index)


def test_classify_market_regimes_outputs_expected_columns() -> None:
    classified = classify_market_regimes(_sample_candles())

    assert "trend_bucket" in classified.columns
    assert "vol_regime" in classified.columns
    assert set(classified["trend_bucket"].dropna().unique()).issubset({"bullish", "bearish", "sideways"})
    assert set(classified["vol_regime"].dropna().unique()).issubset(
        {"high_volatility", "low_volatility", "normal_volatility"}
    )


def test_attach_trade_regimes_and_subset_metrics() -> None:
    regimes = classify_market_regimes(_sample_candles())
    trades = pd.DataFrame(
        [
            {
                "entry_time": regimes.index[20],
                "exit_time": regimes.index[25],
                "pnl": 120.0,
            },
            {
                "entry_time": regimes.index[45],
                "exit_time": regimes.index[50],
                "pnl": -30.0,
            },
            {
                "entry_time": regimes.index[75],
                "exit_time": regimes.index[80],
                "pnl": 80.0,
            },
        ]
    )

    attached = attach_trade_regimes(trades, regimes)
    assert "trend_bucket" in attached.columns
    assert "vol_regime" in attached.columns
    assert attached["trend_bucket"].notna().all()

    profitable_label = str(attached.sort_values("pnl", ascending=False).iloc[0]["trend_bucket"])
    profitable_metrics = metrics_from_trades(attached[attached["trend_bucket"] == profitable_label], 10_000.0)
    assert profitable_metrics["number_of_trades"] >= 1
    assert profitable_metrics["net_profit"] > 0


def test_build_scientific_ranking_requires_cross_context_breadth() -> None:
    rows = [
        {
            "name": "CrossAssetLeader",
            "robustness_score": 74.0,
            "consistency_score": 72.0,
            "asset_robustness": 0.75,
            "timeframe_robustness": 0.66,
            "regime_robustness": 0.60,
            "profit_factor_mean": 1.18,
            "drawdown_max": 0.12,
        },
        {
            "name": "BTCOnly",
            "robustness_score": 78.0,
            "consistency_score": 75.0,
            "asset_robustness": 0.25,
            "timeframe_robustness": 0.50,
            "regime_robustness": 0.40,
            "profit_factor_mean": 1.35,
            "drawdown_max": 0.10,
        },
    ]

    ranked = build_scientific_ranking(rows)

    assert ranked[0]["name"] == "CrossAssetLeader"
    assert ranked[0]["ready_for_prolonged_paper"] is True
    assert ranked[1]["ready_for_prolonged_paper"] is False


def test_cli_parser_edge_discovery_lab(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-discovery-lab",
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--timeframes",
            "5m,1h",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-discovery-lab"
    assert args.symbols == "BTC/USDT,ETH/USDT"
    assert args.timeframes == "5m,1h"