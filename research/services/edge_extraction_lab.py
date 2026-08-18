from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import robustness_analytics
from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.factory import create_strategy
from strategies.registry import list_registered_strategies
from utils.metrics import (
    expectancy_from_pnl,
    max_drawdown_from_equity_curve,
    profit_factor_from_pnl,
    sharpe_from_pnl,
    win_rate_from_pnl,
)


@dataclass(frozen=True)
class EdgeExtractionConfig:
    report_file: str | None = None
    prioritized_strategies: tuple[str, ...] = (
        "Ichimoku Kumo Breakout",
        "ClassicDonchianBreakout",
        "ClassicATRBreakout",
    )
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    timeframes: tuple[str, ...] = ("5m", "15m", "1h")
    window_days: int = 180
    capital: float = 10_000.0
    max_bars: int = 4500
    min_trades_per_filter: int = 20
    top_filters: int = 6
    max_candidate_filters: int = 30
    output_prefix: str = "edge_extraction_lab"


def _canon(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    except (TypeError, ValueError):
        return None
    return None


def _safe_metric(value: float | int | None) -> float:
    parsed = _safe_float(value)
    if parsed is None:
        return 0.0
    return parsed


def _format_threshold(value: float) -> str:
    return f"{value:.6g}"


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _metrics_from_trades(trades: pd.DataFrame, initial_capital: float) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "expectancy": 0.0,
            "drawdown": 0.0,
            "drawdown_pct": 0.0,
            "win_rate": 0.0,
            "return_pct": 0.0,
            "net_profit": 0.0,
        }

    pnl = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0.0).astype(float)
    equity_curve = initial_capital + pnl.cumsum()
    drawdown_abs, drawdown_pct = max_drawdown_from_equity_curve(equity_curve)
    pf = float(profit_factor_from_pnl(pnl))
    if not math.isfinite(pf):
        pf = 999.0

    net_profit = float(pnl.sum())
    return {
        "trades": int(len(trades)),
        "profit_factor": pf,
        "sharpe": float(sharpe_from_pnl(pnl)),
        "expectancy": float(expectancy_from_pnl(pnl)),
        "drawdown": float(drawdown_abs),
        "drawdown_pct": float(drawdown_pct),
        "win_rate": float(win_rate_from_pnl(pnl)),
        "return_pct": float((net_profit / initial_capital) if initial_capital > 0 else 0.0),
        "net_profit": net_profit,
    }


def _compute_indicators(candles: pd.DataFrame) -> pd.DataFrame:
    df = candles.copy().sort_index()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    # EMA block
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    df["ema50"] = ema50
    df["ema200"] = ema200
    df["ema50_slope"] = (ema50 - ema50.shift(5)) / close.replace(0.0, pd.NA)
    df["ema200_slope"] = (ema200 - ema200.shift(5)) / close.replace(0.0, pd.NA)
    df["ema_distance_50_200"] = (ema50 - ema200) / close.replace(0.0, pd.NA)

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.rolling(14, min_periods=14).mean()
    avg_loss = loss.rolling(14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    df["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    # ATR and ADX
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14, min_periods=8).mean()
    df["atr"] = atr14
    df["atr_pct"] = atr14 / close.replace(0.0, pd.NA)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    plus_di = 100.0 * plus_dm.rolling(14, min_periods=8).mean() / atr14.replace(0.0, pd.NA)
    minus_di = 100.0 * minus_dm.rolling(14, min_periods=8).mean() / atr14.replace(0.0, pd.NA)
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, pd.NA)).fillna(0.0)
    df["adx"] = dx.rolling(14, min_periods=8).mean()

    # MACD
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    df["macd"] = macd
    df["macd_signal"] = signal
    df["macd_hist"] = macd - signal

    # Bollinger / volatility / volume
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)
    upper = mid + 2.0 * std
    lower = mid - 2.0 * std
    df["bollinger_width"] = (upper - lower) / mid.replace(0.0, pd.NA)

    log_ret = (close / close.shift(1)).replace(0.0, pd.NA)
    log_ret = log_ret.apply(lambda x: math.log(x) if pd.notna(x) and x > 0 else math.nan)
    df["realized_volatility"] = log_ret.rolling(20, min_periods=10).std(ddof=0) * math.sqrt(20.0)

    vol_mean = volume.rolling(20, min_periods=10).mean()
    df["relative_volume"] = volume / vol_mean.replace(0.0, pd.NA)

    # Regime label
    bullish = (df["ema50"] > df["ema200"]) & (df["ema200_slope"] > 0.0)
    bearish = (df["ema50"] < df["ema200"]) & (df["ema200_slope"] < 0.0)
    df["market_regime"] = "sideways"
    df.loc[bullish, "market_regime"] = "bullish"
    df.loc[bearish, "market_regime"] = "bearish"

    return df


def _attach_entry_features(
    trades: pd.DataFrame,
    candles_with_indicators: pd.DataFrame,
    *,
    strategy_name: str,
    symbol: str,
    timeframe: str,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    frame = trades.copy()
    frame["entry_time"] = _to_utc(frame["entry_time"])
    frame["exit_time"] = _to_utc(frame["exit_time"])
    frame = frame.dropna(subset=["entry_time", "exit_time"]).sort_values("entry_time")
    if frame.empty:
        return frame

    indicators = candles_with_indicators.reset_index().rename(columns={candles_with_indicators.index.name or "index": "timestamp"})
    indicators["timestamp"] = _to_utc(indicators["timestamp"])

    cols = [
        "timestamp",
        "close",
        "adx",
        "atr_pct",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "ema50_slope",
        "ema200_slope",
        "ema_distance_50_200",
        "bollinger_width",
        "realized_volatility",
        "relative_volume",
        "market_regime",
    ]
    merged = pd.merge_asof(
        frame.sort_values("entry_time"),
        indicators[cols].sort_values("timestamp"),
        left_on="entry_time",
        right_on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta("7D"),
    )

    merged["strategy"] = strategy_name
    merged["symbol"] = symbol
    merged["timeframe"] = timeframe
    merged["weekday"] = merged["entry_time"].dt.weekday.astype(float)
    merged["hour"] = merged["entry_time"].dt.hour.astype(float)
    merged["duration_minutes"] = (
        (merged["exit_time"] - merged["entry_time"]).dt.total_seconds().div(60.0).fillna(0.0)
    )

    merged["entry_price"] = pd.to_numeric(merged.get("entry_price"), errors="coerce")
    merged["quantity"] = pd.to_numeric(merged.get("quantity"), errors="coerce")
    merged["pnl"] = pd.to_numeric(merged.get("pnl"), errors="coerce").fillna(0.0)
    merged["return_pct"] = pd.to_numeric(merged.get("pnl_pct"), errors="coerce")

    inferred_return = merged["pnl"] / (merged["entry_price"].abs() * merged["quantity"].abs()).replace(0.0, pd.NA)
    merged["return_pct"] = merged["return_pct"].fillna(inferred_return).fillna(0.0)
    merged["winner"] = (merged["pnl"] > 0.0).astype(int)

    merged["mfe_pct"] = 0.0
    merged["mae_pct"] = 0.0

    candles = candles_with_indicators.sort_index()
    for idx, row in merged.iterrows():
        entry_time = row.get("entry_time")
        exit_time = row.get("exit_time")
        entry_price = _safe_float(row.get("entry_price"))
        if entry_price is None or entry_price <= 0.0:
            entry_price = _safe_float(row.get("close"))
        if entry_price is None or entry_price <= 0.0:
            continue

        path = candles[(candles.index >= entry_time) & (candles.index <= exit_time)]
        if path.empty:
            nearest = candles.loc[:entry_time].tail(1)
            path = nearest
        if path.empty:
            continue

        high_max = _safe_float(pd.to_numeric(path["high"], errors="coerce").max())
        low_min = _safe_float(pd.to_numeric(path["low"], errors="coerce").min())
        if high_max is None or low_min is None:
            continue

        mfe = max(0.0, (high_max - entry_price) / entry_price)
        mae = max(0.0, (entry_price - low_min) / entry_price)
        merged.at[idx, "mfe_pct"] = mfe
        merged.at[idx, "mae_pct"] = mae

    return merged


def _assign_local_profit_factor(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        out = trades.copy()
        out["local_profit_factor"] = 0.0
        return out

    keys = ["strategy", "symbol", "timeframe", "hour"]
    grouped = trades.groupby(keys, dropna=False)
    pf_rows: list[dict[str, Any]] = []
    for key, frame in grouped:
        pnl = pd.to_numeric(frame["pnl"], errors="coerce").fillna(0.0)
        pf = float(profit_factor_from_pnl(pnl))
        if not math.isfinite(pf):
            pf = 999.0
        node = {k: v for k, v in zip(keys, key)}
        node["local_profit_factor"] = pf
        pf_rows.append(node)

    pf_df = pd.DataFrame(pf_rows)
    return trades.merge(pf_df, on=keys, how="left")


def _rank_attributes(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty:
        return []

    numeric_attributes = [
        "adx",
        "atr_pct",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "ema50_slope",
        "ema200_slope",
        "ema_distance_50_200",
        "bollinger_width",
        "realized_volatility",
        "relative_volume",
        "hour",
        "weekday",
        "duration_minutes",
        "mfe_pct",
        "mae_pct",
        "return_pct",
        "local_profit_factor",
    ]

    ranked: list[dict[str, Any]] = []
    wins = trades[trades["winner"] == 1]
    losses = trades[trades["winner"] == 0]
    total = max(1, len(trades))

    for column in numeric_attributes:
        if column not in trades.columns:
            continue
        values = pd.to_numeric(trades[column], errors="coerce")
        valid = values.dropna()
        if len(valid) < 10:
            continue

        w = pd.to_numeric(wins[column], errors="coerce").dropna()
        l = pd.to_numeric(losses[column], errors="coerce").dropna()
        if len(w) < 5 or len(l) < 5:
            continue

        std = float(valid.std(ddof=0))
        if std <= 0.0:
            continue

        win_mean = float(w.mean())
        loss_mean = float(l.mean())
        effect = (win_mean - loss_mean) / std
        coverage = float(len(valid) / total)
        score = abs(effect) * coverage

        ranked.append(
            {
                "attribute": column,
                "attribute_type": "numeric",
                "association_score": round(score, 6),
                "effect_size": round(effect, 6),
                "winner_mean": round(win_mean, 6),
                "loser_mean": round(loss_mean, 6),
                "coverage": round(coverage, 6),
                "direction": "higher_on_winners" if effect >= 0 else "lower_on_winners",
            }
        )

    # Categorical contexts as candidate filters with lift
    for column in ["market_regime"]:
        if column not in trades.columns:
            continue
        base_wr = float(trades["winner"].mean())
        for level, frame in trades.groupby(column):
            if len(frame) < 10:
                continue
            wr = float(frame["winner"].mean())
            lift = wr - base_wr
            coverage = float(len(frame) / total)
            score = abs(lift) * math.sqrt(max(1, len(frame))) * coverage
            ranked.append(
                {
                    "attribute": column,
                    "attribute_type": "categorical",
                    "level": str(level),
                    "association_score": round(score, 6),
                    "effect_size": round(lift, 6),
                    "winner_mean": round(wr, 6),
                    "loser_mean": round(1.0 - wr, 6),
                    "coverage": round(coverage, 6),
                    "direction": "level_favors_winners" if lift >= 0 else "level_favors_losers",
                }
            )

    ranked.sort(key=lambda row: float(row.get("association_score") or 0.0), reverse=True)
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _apply_filter(trades: pd.DataFrame, filter_row: dict[str, Any]) -> pd.Series:
    attribute = str(filter_row.get("attribute"))
    operator = str(filter_row.get("operator"))

    if attribute == "market_regime":
        level = str(filter_row.get("level"))
        return trades[attribute].astype(str) == level

    values = pd.to_numeric(trades[attribute], errors="coerce")
    threshold = float(filter_row.get("threshold"))
    if operator == ">":
        return values > threshold
    if operator == "<=":
        return values <= threshold
    return pd.Series([False] * len(trades), index=trades.index)


def _build_filter_candidates(
    trades: pd.DataFrame,
    ranking: list[dict[str, Any]],
    base_metrics: dict[str, float | int],
    cfg: EdgeExtractionConfig,
) -> list[dict[str, Any]]:
    if trades.empty:
        return []

    rows: list[dict[str, Any]] = []
    total_trades = len(trades)

    # Numeric thresholds from strongest numeric attributes
    numeric_ranked = [row for row in ranking if row.get("attribute_type") == "numeric"]
    numeric_ranked = numeric_ranked[:12]
    for node in numeric_ranked:
        attribute = str(node.get("attribute"))
        values = pd.to_numeric(trades[attribute], errors="coerce").dropna()
        if len(values) < max(20, cfg.min_trades_per_filter):
            continue

        quantiles = sorted({
            float(values.quantile(0.20)),
            float(values.quantile(0.35)),
            float(values.quantile(0.50)),
            float(values.quantile(0.65)),
            float(values.quantile(0.80)),
        })

        for threshold in quantiles:
            for operator in (">", "<="):
                filter_row = {"attribute": attribute, "operator": operator, "threshold": threshold}
                mask = _apply_filter(trades, filter_row)
                subset = trades[mask]
                if len(subset) < cfg.min_trades_per_filter or len(subset) >= total_trades:
                    continue

                metrics = _metrics_from_trades(subset, cfg.capital)
                pf_gain = _safe_metric(metrics["profit_factor"]) - _safe_metric(base_metrics["profit_factor"])
                exp_gain = _safe_metric(metrics["expectancy"]) - _safe_metric(base_metrics["expectancy"])
                sharpe_gain = _safe_metric(metrics["sharpe"]) - _safe_metric(base_metrics["sharpe"])
                dd_gain = _safe_metric(base_metrics["drawdown_pct"]) - _safe_metric(metrics["drawdown_pct"])
                coverage = float(len(subset) / total_trades)
                impact_score = (0.45 * pf_gain) + (0.25 * exp_gain) + (0.20 * sharpe_gain) + (0.10 * dd_gain)
                impact_score *= max(0.20, coverage)

                if pf_gain <= 0.0 and exp_gain <= 0.0 and sharpe_gain <= 0.0:
                    continue

                rows.append(
                    {
                        "attribute": attribute,
                        "operator": operator,
                        "threshold": threshold,
                        "level": None,
                        "rule": f"{attribute} {operator} {_format_threshold(threshold)}",
                        "coverage": round(coverage, 6),
                        "trades": int(metrics["trades"]),
                        "profit_factor": round(_safe_metric(metrics["profit_factor"]), 6),
                        "sharpe": round(_safe_metric(metrics["sharpe"]), 6),
                        "expectancy": round(_safe_metric(metrics["expectancy"]), 6),
                        "drawdown_pct": round(_safe_metric(metrics["drawdown_pct"]), 6),
                        "win_rate": round(_safe_metric(metrics["win_rate"]), 6),
                        "pf_delta": round(pf_gain, 6),
                        "expectancy_delta": round(exp_gain, 6),
                        "sharpe_delta": round(sharpe_gain, 6),
                        "drawdown_delta": round(dd_gain, 6),
                        "impact_score": round(impact_score, 6),
                        "source": "numeric_threshold",
                    }
                )

    # Categorical direct filters from ranking levels
    cat_ranked = [row for row in ranking if row.get("attribute_type") == "categorical"]
    for node in cat_ranked:
        attribute = str(node.get("attribute"))
        level = str(node.get("level"))
        filter_row = {"attribute": attribute, "operator": "==", "level": level}
        mask = _apply_filter(trades, filter_row)
        subset = trades[mask]
        if len(subset) < cfg.min_trades_per_filter or len(subset) >= total_trades:
            continue

        metrics = _metrics_from_trades(subset, cfg.capital)
        pf_gain = _safe_metric(metrics["profit_factor"]) - _safe_metric(base_metrics["profit_factor"])
        exp_gain = _safe_metric(metrics["expectancy"]) - _safe_metric(base_metrics["expectancy"])
        sharpe_gain = _safe_metric(metrics["sharpe"]) - _safe_metric(base_metrics["sharpe"])
        dd_gain = _safe_metric(base_metrics["drawdown_pct"]) - _safe_metric(metrics["drawdown_pct"])
        coverage = float(len(subset) / total_trades)
        impact_score = ((0.45 * pf_gain) + (0.25 * exp_gain) + (0.20 * sharpe_gain) + (0.10 * dd_gain)) * max(0.20, coverage)

        if pf_gain <= 0.0 and exp_gain <= 0.0 and sharpe_gain <= 0.0:
            continue

        rows.append(
            {
                "attribute": attribute,
                "operator": "==",
                "threshold": None,
                "level": level,
                "rule": f"{attribute} == {level}",
                "coverage": round(coverage, 6),
                "trades": int(metrics["trades"]),
                "profit_factor": round(_safe_metric(metrics["profit_factor"]), 6),
                "sharpe": round(_safe_metric(metrics["sharpe"]), 6),
                "expectancy": round(_safe_metric(metrics["expectancy"]), 6),
                "drawdown_pct": round(_safe_metric(metrics["drawdown_pct"]), 6),
                "win_rate": round(_safe_metric(metrics["win_rate"]), 6),
                "pf_delta": round(pf_gain, 6),
                "expectancy_delta": round(exp_gain, 6),
                "sharpe_delta": round(sharpe_gain, 6),
                "drawdown_delta": round(dd_gain, 6),
                "impact_score": round(impact_score, 6),
                "source": "categorical_level",
            }
        )

    rows.sort(key=lambda row: float(row.get("impact_score") or 0.0), reverse=True)

    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("rule"))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
        if len(dedup) >= cfg.max_candidate_filters:
            break

    selected = dedup[: cfg.top_filters]
    for idx, row in enumerate(selected, start=1):
        row["selected_rank"] = idx
    return selected


def _incremental_simulation(
    trades: pd.DataFrame,
    selected_filters: list[dict[str, Any]],
    cfg: EdgeExtractionConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_mask = pd.Series([True] * len(trades), index=trades.index)

    base_metrics = _metrics_from_trades(trades, cfg.capital)
    rows.append(
        {
            "step": 0,
            "applied_filter": "baseline",
            **base_metrics,
        }
    )

    for idx, filter_row in enumerate(selected_filters, start=1):
        current_mask = current_mask & _apply_filter(trades, filter_row)
        subset = trades[current_mask]
        metrics = _metrics_from_trades(subset, cfg.capital)
        rows.append(
            {
                "step": idx,
                "applied_filter": str(filter_row.get("rule")),
                **metrics,
            }
        )

        if len(subset) < cfg.min_trades_per_filter:
            break

    return rows


def _sensitivity_analysis(
    trades: pd.DataFrame,
    selected_filters: list[dict[str, Any]],
    cfg: EdgeExtractionConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for filter_row in selected_filters:
        attribute = str(filter_row.get("attribute"))
        operator = str(filter_row.get("operator"))
        if operator == "==":
            continue

        values = pd.to_numeric(trades[attribute], errors="coerce").dropna()
        if len(values) < max(25, cfg.min_trades_per_filter):
            continue

        thresholds = [float(values.quantile(q)) for q in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)]
        points: list[dict[str, Any]] = []
        for threshold in thresholds:
            probe = {
                "attribute": attribute,
                "operator": operator,
                "threshold": threshold,
            }
            subset = trades[_apply_filter(trades, probe)]
            metrics = _metrics_from_trades(subset, cfg.capital)
            points.append(
                {
                    "threshold": threshold,
                    "trades": int(metrics["trades"]),
                    "profit_factor": round(_safe_metric(metrics["profit_factor"]), 6),
                    "expectancy": round(_safe_metric(metrics["expectancy"]), 6),
                    "sharpe": round(_safe_metric(metrics["sharpe"]), 6),
                    "drawdown_pct": round(_safe_metric(metrics["drawdown_pct"]), 6),
                }
            )

        stable = [
            p for p in points
            if int(p["trades"]) >= cfg.min_trades_per_filter and float(p["profit_factor"]) >= 1.0 and float(p["expectancy"]) > 0.0
        ]
        stability = float(len(stable) / max(1, len(points)))
        low = min((float(p["threshold"]) for p in stable), default=None)
        high = max((float(p["threshold"]) for p in stable), default=None)

        rows.append(
            {
                "attribute": attribute,
                "operator": operator,
                "base_threshold": _safe_float(filter_row.get("threshold")),
                "stability_score": round(stability, 6),
                "optimal_low": low,
                "optimal_high": high,
                "stable_points": len(stable),
                "total_points": len(points),
                "points": points,
            }
        )

    rows.sort(key=lambda row: float(row.get("stability_score") or 0.0), reverse=True)
    return rows


class EdgeExtractionLabService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: EdgeExtractionConfig) -> dict[str, Any]:
        campaigns = robustness_analytics._load_phase13_campaigns()
        if not campaigns:
            raise RuntimeError("Nenhuma campanha da FASE 13 encontrada para Edge Extraction Lab.")

        reference_report = self._load_reference_report(cfg, campaigns)
        backlog = reference_report.get("backlog", []) if isinstance(reference_report.get("backlog"), list) else []
        selected = self._select_prioritized_strategies(backlog, cfg)
        if not selected:
            raise RuntimeError("Nenhuma estrategia prioritaria elegivel foi encontrada.")

        all_features: list[pd.DataFrame] = []
        context_rows: list[dict[str, Any]] = []
        for item in selected:
            strategy_name = str(item.get("platform_strategy_name") or item.get("candidate_name") or "").strip()
            if not strategy_name:
                continue

            for symbol in cfg.symbols:
                for timeframe in cfg.timeframes:
                    candles = self._load_market_data(symbol, timeframe, cfg.window_days, cfg.max_bars)
                    if candles.empty:
                        continue

                    indicators = _compute_indicators(candles)
                    backtest = self._run_backtest(strategy_name, symbol, timeframe, candles, cfg.capital)
                    trades = backtest.get("trades", pd.DataFrame())
                    metrics = _metrics_from_trades(trades, cfg.capital)
                    context_rows.append(
                        {
                            "strategy": strategy_name,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            **metrics,
                        }
                    )
                    if trades.empty:
                        continue

                    features = _attach_entry_features(
                        trades,
                        indicators,
                        strategy_name=strategy_name,
                        symbol=symbol,
                        timeframe=timeframe,
                    )
                    if not features.empty:
                        all_features.append(features)

        if not all_features:
            raise RuntimeError("Edge Extraction Lab nao encontrou trades para analise na janela configurada.")

        trades_df = pd.concat(all_features, ignore_index=True)
        trades_df = _assign_local_profit_factor(trades_df)

        ranking = _rank_attributes(trades_df)
        base_metrics = _metrics_from_trades(trades_df, cfg.capital)
        selected_filters = _build_filter_candidates(trades_df, ranking, base_metrics, cfg)
        incremental = _incremental_simulation(trades_df, selected_filters, cfg)
        sensitivity = _sensitivity_analysis(trades_df, selected_filters, cfg)

        edge_candidate, rejection_reasons, refinement = self._edge_decision(
            selected_filters=selected_filters,
            incremental=incremental,
            sensitivity=sensitivity,
        )

        summary = {
            "selected_strategies": [str(item.get("platform_strategy_name") or item.get("candidate_name")) for item in selected],
            "contexts_tested": int(len(context_rows)),
            "trades_analyzed": int(len(trades_df)),
            "base_profit_factor": round(_safe_metric(base_metrics.get("profit_factor")), 6),
            "base_sharpe": round(_safe_metric(base_metrics.get("sharpe")), 6),
            "base_expectancy": round(_safe_metric(base_metrics.get("expectancy")), 6),
            "top_filter_count": int(len(selected_filters)),
            "edge_candidate_for_paper_approved": bool(edge_candidate),
            "rejection_reasons": rejection_reasons,
            "refinement_proposal": refinement,
            "candidate_ready_reference": {
                "profit_factor_target": 1.15,
                "expectancy_target": 0.0,
                "sharpe_target": 0.0,
            },
        }

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "mission": "Find robust operational edge with statistically justified filters.",
            "architecture_freeze": {
                "portfolio_manager": "frozen",
                "new_routers": "frozen",
                "machine_learning": "frozen",
                "new_dashboards": "frozen",
                "new_apis": "frozen",
                "new_monitoring_modules": "frozen",
            },
            "summary": summary,
            "contexts": context_rows,
            "attribute_ranking": ranking,
            "candidate_filters": selected_filters,
            "incremental_simulation": incremental,
            "sensitivity_analysis": sensitivity,
            "decision": {
                "edge_candidate_for_paper_approved": bool(edge_candidate),
                "rejection_reasons": rejection_reasons,
                "refinement_proposal": refinement,
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report, ranking, selected_filters, incremental, sensitivity, trades_df)
        return {
            "summary": summary,
            "report": report,
            "outputs": outputs,
        }

    def _edge_decision(
        self,
        *,
        selected_filters: list[dict[str, Any]],
        incremental: list[dict[str, Any]],
        sensitivity: list[dict[str, Any]],
    ) -> tuple[bool, list[str], dict[str, Any] | None]:
        if len(incremental) < 2:
            return False, ["no_incremental_improvement"], None

        final = incremental[-1]
        pf = _safe_metric(final.get("profit_factor"))
        sharpe = _safe_metric(final.get("sharpe"))
        expectancy = _safe_metric(final.get("expectancy"))
        trades = int(final.get("trades") or 0)
        stable_filters = [row for row in sensitivity if float(row.get("stability_score") or 0.0) >= 0.40]

        reasons: list[str] = []
        if pf <= 1.15:
            reasons.append("profit_factor_below_target")
        if sharpe <= 0.0:
            reasons.append("sharpe_not_positive")
        if expectancy <= 0.0:
            reasons.append("expectancy_not_positive")
        if trades < 20:
            reasons.append("insufficient_trade_count_after_filters")
        if not stable_filters:
            reasons.append("no_stable_filter_region")

        if reasons:
            return False, reasons, None

        return (
            True,
            [],
            {
                "recommended_filters": [str(row.get("rule")) for row in selected_filters],
                "final_incremental_metrics": {
                    "profit_factor": round(pf, 6),
                    "sharpe": round(sharpe, 6),
                    "expectancy": round(expectancy, 6),
                    "trades": trades,
                },
                "stable_regions": [
                    {
                        "attribute": row.get("attribute"),
                        "operator": row.get("operator"),
                        "optimal_low": row.get("optimal_low"),
                        "optimal_high": row.get("optimal_high"),
                        "stability_score": row.get("stability_score"),
                    }
                    for row in stable_filters
                ],
            },
        )

    def _load_reference_report(
        self,
        cfg: EdgeExtractionConfig,
        campaigns: list[robustness_analytics.Campaign],
    ) -> dict[str, Any]:
        if cfg.report_file:
            payload = json.loads(Path(cfg.report_file).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Arquivo de referencia invalido para Edge Extraction Lab.")
            return payload
        return campaigns[-1].payload

    def _strategy_alias_map(self) -> dict[str, str]:
        strategies = list_registered_strategies()
        by_name = {_canon(str(s.get("name", ""))): str(s.get("name")) for s in strategies}
        aliases = {
            "ichimokukumobreakout": "ClassicDonchianBreakout",
            "ichimokukumobreakoutstrategy": "ClassicDonchianBreakout",
            "classicdonchianbreakout": "ClassicDonchianBreakout",
            "classicatrbreakout": "ClassicATRBreakout",
        }
        out = dict(by_name)
        for key, value in aliases.items():
            if _canon(value) in by_name:
                out[key] = value
        return out

    def _select_prioritized_strategies(
        self,
        backlog: list[dict[str, Any]],
        cfg: EdgeExtractionConfig,
    ) -> list[dict[str, Any]]:
        alias_map = self._strategy_alias_map()
        priority_keys = {_canon(name) for name in cfg.prioritized_strategies}

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        for row in backlog:
            if not isinstance(row, dict):
                continue
            candidate = str(row.get("candidate_name") or "").strip()
            platform_name = str(row.get("platform_strategy_name") or candidate).strip()
            candidate_key = _canon(candidate)
            platform_key = _canon(platform_name)
            normalized_candidate = _canon(alias_map.get(candidate_key, candidate))
            normalized_platform = _canon(alias_map.get(platform_key, platform_name))
            if normalized_candidate not in priority_keys and normalized_platform not in priority_keys:
                continue

            canonical_platform = alias_map.get(platform_key) or alias_map.get(candidate_key) or platform_name or candidate
            canonical_key = _canon(canonical_platform)
            if canonical_key in seen:
                continue
            seen.add(canonical_key)
            selected.append(
                {
                    **row,
                    "candidate_name": candidate or canonical_platform,
                    "platform_strategy_name": canonical_platform,
                }
            )

        # Add missing priorities if they are registered even if absent from backlog.
        for priority in cfg.prioritized_strategies:
            key = _canon(priority)
            canonical = alias_map.get(key)
            if not canonical:
                continue
            canonical_key = _canon(canonical)
            if canonical_key in seen:
                continue
            seen.add(canonical_key)
            selected.append(
                {
                    "candidate_name": priority,
                    "platform_strategy_name": canonical,
                    "state": "PRIORITY_FOR_EDGE_EXTRACTION",
                    "queue_score": 0.0,
                }
            )

        return selected

    def _load_market_data(self, symbol: str, timeframe: str, window_days: int, max_bars: int) -> pd.DataFrame:
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(10, int(window_days)))
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start_dt, end_dt)

        if not candles:
            return pd.DataFrame()

        frame = pd.DataFrame(
            [
                {
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ],
            index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
        )
        return frame.tail(max(500, int(max_bars))).copy()

    def _run_backtest(self, strategy_name: str, symbol: str, timeframe: str, candles: pd.DataFrame, capital: float) -> dict[str, Any]:
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        result = BacktestEngine(strategy, config=BacktestConfig(initial_capital=capital)).run(
            candles,
            symbol=symbol,
            timeframe=timeframe,
        )
        trades = pd.DataFrame(result.trades)
        if not trades.empty:
            trades["entry_time"] = _to_utc(trades["entry_time"])
            trades["exit_time"] = _to_utc(trades["exit_time"])
            trades = trades.dropna(subset=["entry_time", "exit_time"]).sort_values("entry_time")
        return {
            "trades": trades,
            "metrics": result.metrics.to_dict(),
        }

    def _write_outputs(
        self,
        output_prefix: str,
        report: dict[str, Any],
        ranking: list[dict[str, Any]],
        filters: list[dict[str, Any]],
        incremental: list[dict[str, Any]],
        sensitivity: list[dict[str, Any]],
        trades_df: pd.DataFrame,
    ) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        ranking_csv = self._results_dir / f"{output_prefix}_{stamp}_attribute_ranking.csv"
        filters_csv = self._results_dir / f"{output_prefix}_{stamp}_candidate_filters.csv"
        incremental_csv = self._results_dir / f"{output_prefix}_{stamp}_incremental_simulation.csv"
        sensitivity_csv = self._results_dir / f"{output_prefix}_{stamp}_sensitivity.csv"
        trades_csv = self._results_dir / f"{output_prefix}_{stamp}_trade_features.csv"
        executive_md = self._results_dir / f"{output_prefix}_{stamp}_executive_summary.md"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._render_markdown(report), encoding="utf-8")
        self._write_csv(ranking_csv, ranking)
        self._write_csv(filters_csv, filters)
        self._write_csv(incremental_csv, incremental)
        self._write_csv(
            sensitivity_csv,
            [
                {
                    "attribute": row.get("attribute"),
                    "operator": row.get("operator"),
                    "base_threshold": row.get("base_threshold"),
                    "stability_score": row.get("stability_score"),
                    "optimal_low": row.get("optimal_low"),
                    "optimal_high": row.get("optimal_high"),
                    "stable_points": row.get("stable_points"),
                    "total_points": row.get("total_points"),
                }
                for row in sensitivity
            ],
        )
        trades_df.to_csv(trades_csv, index=False)
        executive_md.write_text(self._render_executive_summary(report), encoding="utf-8")

        return {
            "json": str(json_path),
            "md": str(md_path),
            "attribute_ranking_csv": str(ranking_csv),
            "candidate_filters_csv": str(filters_csv),
            "incremental_simulation_csv": str(incremental_csv),
            "sensitivity_csv": str(sensitivity_csv),
            "trade_features_csv": str(trades_csv),
            "executive_summary_md": str(executive_md),
        }

    def _write_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _render_markdown(self, report: dict[str, Any]) -> str:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        ranking = report.get("attribute_ranking", []) if isinstance(report.get("attribute_ranking"), list) else []
        filters = report.get("candidate_filters", []) if isinstance(report.get("candidate_filters"), list) else []
        simulation = report.get("incremental_simulation", []) if isinstance(report.get("incremental_simulation"), list) else []
        sensitivity = report.get("sensitivity_analysis", []) if isinstance(report.get("sensitivity_analysis"), list) else []

        lines = [
            "# Edge Extraction Lab",
            "",
            "## Mission",
            "- Find statistically defensible operational edge after costs.",
            "- Keep architecture frozen while searching for robust profitability.",
            "",
            "## Summary",
            f"- Selected strategies: {', '.join(summary.get('selected_strategies', [])) or 'none'}",
            f"- Contexts tested: {summary.get('contexts_tested')}",
            f"- Trades analyzed: {summary.get('trades_analyzed')}",
            f"- Base Profit Factor: {summary.get('base_profit_factor')}",
            f"- Base Sharpe: {summary.get('base_sharpe')}",
            f"- Base Expectancy: {summary.get('base_expectancy')}",
            "",
            "## Attribute Ranking (Top 10)",
        ]
        for row in ranking[:10]:
            lines.append(
                f"- #{row.get('rank')} {row.get('attribute')} | score={row.get('association_score')} | direction={row.get('direction')}"
            )

        lines.extend(["", "## Candidate Filters"]) 
        if filters:
            for row in filters:
                lines.append(
                    f"- {row.get('rule')} | impact={row.get('impact_score')} | PF={row.get('profit_factor')} | Expectancy={row.get('expectancy')} | Trades={row.get('trades')}"
                )
        else:
            lines.append("- No filter met minimum statistical impact in this run.")

        lines.extend(["", "## Incremental Simulation"])
        for row in simulation:
            lines.append(
                f"- Step {row.get('step')}: {row.get('applied_filter')} | PF={row.get('profit_factor')} | Sharpe={row.get('sharpe')} | Expectancy={row.get('expectancy')} | Drawdown={row.get('drawdown_pct')} | WinRate={row.get('win_rate')} | Trades={row.get('trades')}"
            )

        lines.extend(["", "## Sensitivity Analysis"])
        if sensitivity:
            for row in sensitivity:
                lines.append(
                    f"- {row.get('attribute')} ({row.get('operator')}) | stability={row.get('stability_score')} | optimal_range=[{row.get('optimal_low')}, {row.get('optimal_high')}]"
                )
        else:
            lines.append("- No numeric filter with enough support for sensitivity analysis.")

        lines.extend(
            [
                "",
                "## Success Reference",
                "- Profit Factor > 1.15",
                "- Sharpe > 0",
                "- Expectancy > 0",
                "- Positive net return with controlled drawdown",
                "- Persistence in rolling out-of-sample",
            ]
        )

        return "\n".join(lines) + "\n"

    def _render_executive_summary(self, report: dict[str, Any]) -> str:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        decision = report.get("decision", {}) if isinstance(report.get("decision"), dict) else {}
        approved = bool(decision.get("edge_candidate_for_paper_approved"))

        lines = [
            "# EDGE-01 Executive Summary",
            "",
            f"- Selected strategies: {', '.join(summary.get('selected_strategies', [])) or 'none'}",
            f"- Trades analyzed: {summary.get('trades_analyzed', 0)}",
            f"- Base Profit Factor: {summary.get('base_profit_factor', 0.0)}",
            f"- Base Sharpe: {summary.get('base_sharpe', 0.0)}",
            f"- Base Expectancy: {summary.get('base_expectancy', 0.0)}",
            "",
            "## Scientific Answer",
            f"- Is there a filter set that can transform an existing strategy into PAPER_APPROVED candidate? {'SIM' if approved else 'NAO'}",
        ]

        if approved:
            proposal = decision.get("refinement_proposal", {}) if isinstance(decision.get("refinement_proposal"), dict) else {}
            lines.extend(
                [
                    "",
                    "## Refinement Proposal",
                    f"- Recommended filters: {', '.join(proposal.get('recommended_filters', [])) or 'none'}",
                    f"- Final metrics: {proposal.get('final_incremental_metrics', {})}",
                ]
            )
        else:
            reasons = decision.get("rejection_reasons", []) if isinstance(decision.get("rejection_reasons"), list) else []
            lines.extend(
                [
                    "",
                    "## Rejection Reasons",
                ]
            )
            if reasons:
                for reason in reasons:
                    lines.append(f"- {reason}")
            else:
                lines.append("- none")

        return "\n".join(lines) + "\n"
