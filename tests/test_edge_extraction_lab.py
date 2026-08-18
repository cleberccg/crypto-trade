from __future__ import annotations

import pandas as pd

from main import _parse_args
from research.services.edge_extraction_lab import (
    EdgeExtractionConfig,
    _build_filter_candidates,
    _incremental_simulation,
    _metrics_from_trades,
    _rank_attributes,
)


def _synthetic_trades() -> pd.DataFrame:
    rows = []
    base_time = pd.Timestamp("2024-01-01T00:00:00Z")
    for i in range(120):
        winner = i % 3 != 0
        pnl = 80.0 if winner else -55.0
        rows.append(
            {
                "entry_time": base_time + pd.Timedelta(minutes=5 * i),
                "exit_time": base_time + pd.Timedelta(minutes=5 * i + 15),
                "pnl": pnl,
                "winner": int(winner),
                "strategy": "ClassicDonchianBreakout",
                "symbol": "BTC/USDT",
                "timeframe": "5m",
                "hour": float((i // 12) % 24),
                "weekday": float((i // 24) % 7),
                "adx": 32.0 if winner else 17.0,
                "atr_pct": 0.018 if winner else 0.010,
                "rsi": 58.0 if winner else 44.0,
                "macd": 0.9 if winner else -0.4,
                "macd_signal": 0.6 if winner else -0.2,
                "macd_hist": 0.3 if winner else -0.2,
                "ema50_slope": 0.004 if winner else -0.002,
                "ema200_slope": 0.0018 if winner else -0.0011,
                "ema_distance_50_200": 0.015 if winner else -0.010,
                "bollinger_width": 0.09 if winner else 0.05,
                "realized_volatility": 0.04 if winner else 0.025,
                "relative_volume": 1.30 if winner else 0.82,
                "duration_minutes": 22.0 if winner else 35.0,
                "mfe_pct": 0.018 if winner else 0.005,
                "mae_pct": 0.004 if winner else 0.021,
                "return_pct": 0.012 if winner else -0.009,
                "local_profit_factor": 1.8 if winner else 0.7,
                "market_regime": "bullish" if winner else "sideways",
            }
        )
    return pd.DataFrame(rows)


def test_edge_extraction_ranking_and_filters_have_signal() -> None:
    trades = _synthetic_trades()
    ranking = _rank_attributes(trades)
    assert ranking
    top = ranking[0]
    assert top["association_score"] > 0

    base_metrics = _metrics_from_trades(trades, 10_000.0)
    cfg = EdgeExtractionConfig(min_trades_per_filter=15, top_filters=4, max_candidate_filters=20)
    filters = _build_filter_candidates(trades, ranking, base_metrics, cfg)

    assert filters
    assert all("rule" in row for row in filters)
    assert any(float(row.get("impact_score", 0.0)) > 0.0 for row in filters)


def test_edge_extraction_incremental_simulation_has_baseline_and_steps() -> None:
    trades = _synthetic_trades()
    ranking = _rank_attributes(trades)
    base_metrics = _metrics_from_trades(trades, 10_000.0)
    cfg = EdgeExtractionConfig(min_trades_per_filter=15, top_filters=3, max_candidate_filters=12)
    filters = _build_filter_candidates(trades, ranking, base_metrics, cfg)
    simulation = _incremental_simulation(trades, filters, cfg)

    assert simulation
    assert simulation[0]["step"] == 0
    assert simulation[0]["applied_filter"] == "baseline"
    assert len(simulation) >= 2


def test_cli_parser_edge_extraction_lab(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-extraction-lab",
            "--prioritized-strategies",
            "ClassicDonchianBreakout,ClassicATRBreakout",
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--timeframes",
            "5m,1h",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-extraction-lab"
    assert args.prioritized_strategies == "ClassicDonchianBreakout,ClassicATRBreakout"
    assert args.symbols == "BTC/USDT,ETH/USDT"
    assert args.timeframes == "5m,1h"
