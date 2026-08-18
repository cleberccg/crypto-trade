from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from investigacao_cdb_edge_loss import Config as InvestigationConfig
from investigacao_cdb_edge_loss import _attach_entry_features
from investigacao_cdb_edge_loss import _load_candles_for_contexts
from investigacao_cdb_edge_loss import _trade_path_metrics


@dataclass(frozen=True)
class Config:
    strategy_key: str = "ClassicDonchianBreakout@v1.0"
    random_seed: int = 42
    bootstrap_iterations: int = 3_000
    rolling_windows: tuple[int, ...] = (30, 50, 75, 100)
    low_robustness_threshold: int = 30
    high_robustness_threshold: int = 50


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        out = float(v)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default


def _profit_factor_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss <= 0.0:
        return 999.0 if gross_profit > 0.0 else 0.0
    return gross_profit / gross_loss


def _win_rate_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    return float(np.mean(pnl > 0))


def _expectancy_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    return float(np.mean(pnl))


def _drawdown_from_pnl(pnl: np.ndarray, initial: float = 10_000.0) -> float:
    if pnl.size == 0:
        return 0.0
    equity = initial + np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    peak = np.where(peak == 0.0, np.nan, peak)
    dd = (peak - equity) / peak
    dd = np.nan_to_num(dd, nan=0.0, posinf=0.0, neginf=0.0)
    return float(np.max(dd))


def _net_profit_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    return float(np.sum(pnl))


def _cv(values: pd.Series) -> float:
    arr = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if arr.size <= 1:
        return 0.0
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if abs(mean) <= 1e-12:
        return 0.0 if std <= 1e-12 else 999.0
    return abs(std / mean)


def _rolling_metrics(df: pd.DataFrame, window: int) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    if len(df) < window:
        return pd.DataFrame(records)

    ordered = df.sort_values("entry_time").reset_index(drop=True)
    pnl_series = pd.to_numeric(ordered["pnl"], errors="coerce").fillna(0.0)
    for idx in range(window - 1, len(ordered)):
        part = ordered.iloc[idx - window + 1 : idx + 1]
        pnl = pnl_series.iloc[idx - window + 1 : idx + 1].to_numpy(dtype=float)
        records.append(
            {
                "symbol": str(part["symbol"].iloc[-1]),
                "timeframe": str(part["timeframe"].iloc[-1]),
                "window": window,
                "end_idx": idx,
                "end_time": part["exit_time"].iloc[-1],
                "profit_factor": _profit_factor_from_pnl(pnl),
                "win_rate": _win_rate_from_pnl(pnl),
                "expectancy": _expectancy_from_pnl(pnl),
                "drawdown": _drawdown_from_pnl(pnl),
                "net_profit": _net_profit_from_pnl(pnl),
                "cumulative_return": float(pd.to_numeric(part["pnl_percent"], errors="coerce").fillna(0.0).sum()),
            }
        )
    return pd.DataFrame(records)


def _load_all_trades(strategy_key: str) -> pd.DataFrame:
    with get_session() as session:
        stmt = text(
            """
            SELECT
                th.id,
                th.execution_id,
                th.strategy,
                th.symbol,
                th.timeframe,
                th.side,
                th.entry_time,
                th.exit_time,
                th.entry_price,
                th.exit_price,
                th.stop_loss,
                th.take_profit,
                th.quantity,
                th.pnl,
                th.pnl_percent,
                th.duration_minutes,
                th.exit_reason,
                th.score,
                ss.id AS signal_id,
                ss.market_regime,
                ihs.ema_fast AS snap_ema_fast,
                ihs.ema_slow AS snap_ema_slow,
                ihs.ema_trend AS snap_ema_trend,
                ihs.rsi AS snap_rsi,
                ihs.atr AS snap_atr,
                ihs.volume AS snap_volume,
                ihs.volume_average AS snap_volume_avg,
                ihs.close AS snap_close,
                ihs.high AS snap_high,
                ihs.low AS snap_low
            FROM trade_history th
            LEFT JOIN signal_snapshots ss
              ON ss.execution_id = th.execution_id
             AND ss.strategy = th.strategy
             AND ss.symbol = th.symbol
             AND ss.timeframe = th.timeframe
             AND ss.timestamp = th.entry_time
             AND ss.signal = 'BUY'
            LEFT JOIN indicator_snapshots ihs
              ON ihs.signal_id = ss.id
            WHERE th.strategy = :strategy
              AND th.exit_time IS NOT NULL
            ORDER BY th.symbol, th.timeframe, th.entry_time
            """
        )
        rows = session.execute(stmt, {"strategy": strategy_key}).mappings().all()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    for col in ["entry_time", "exit_time"]:
        df[col] = pd.to_datetime(df[col], utc=True)
    num_cols = [
        "entry_price",
        "exit_price",
        "stop_loss",
        "take_profit",
        "quantity",
        "pnl",
        "pnl_percent",
        "duration_minutes",
        "score",
        "snap_ema_fast",
        "snap_ema_slow",
        "snap_ema_trend",
        "snap_rsi",
        "snap_atr",
        "snap_volume",
        "snap_volume_avg",
        "snap_close",
        "snap_high",
        "snap_low",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["result"] = np.where(df["pnl"] > 0, "WIN", "LOSS")
    return df


def _load_candle_coverage(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["symbol", "timeframe", "first_candle", "last_candle", "candle_count"])

    rows: list[dict[str, Any]] = []
    with get_session() as session:
        for symbol, timeframe in trades[["symbol", "timeframe"]].drop_duplicates().itertuples(index=False):
            stmt = text(
                """
                SELECT
                    MIN(open_time) AS first_candle,
                    MAX(open_time) AS last_candle,
                    COUNT(*) AS candle_count
                FROM candles
                WHERE timeframe = :timeframe
                  AND (symbol = :symbol OR symbol = :symbol_alt)
                """
            )
            row = session.execute(
                stmt,
                {
                    "timeframe": str(timeframe),
                    "symbol": str(symbol),
                    "symbol_alt": str(symbol).replace("/", ""),
                },
            ).mappings().first()
            rows.append(
                {
                    "symbol": str(symbol),
                    "timeframe": str(timeframe),
                    "first_candle": None if row is None else row["first_candle"],
                    "last_candle": None if row is None else row["last_candle"],
                    "candle_count": 0 if row is None else int(row["candle_count"] or 0),
                }
            )
    out = pd.DataFrame(rows)
    if not out.empty:
        for col in ["first_candle", "last_candle"]:
            out[col] = pd.to_datetime(out[col], utc=True)
    return out


def _enrich_all_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    candles_by_ctx = _load_candles_for_contexts(trades)
    enriched = _attach_entry_features(trades, candles_by_ctx, InvestigationConfig())
    enriched = _trade_path_metrics(enriched, candles_by_ctx, InvestigationConfig())
    return enriched.sort_values(["symbol", "timeframe", "entry_time"]).reset_index(drop=True)


def _historical_window_table(trades: pd.DataFrame, candle_coverage: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        trades.groupby(["symbol", "timeframe"], dropna=False)
        .agg(
            first_trade=("entry_time", "min"),
            last_trade=("exit_time", "max"),
            trades=("id", "count"),
        )
        .reset_index()
    )
    out = grouped.merge(candle_coverage, on=["symbol", "timeframe"], how="left")
    out["trade_window_days"] = (out["last_trade"] - out["first_trade"]).dt.total_seconds() / 86400.0
    out["data_limitation"] = np.where(
        out["trades"] < 30,
        "Historico de trades curto para conclusoes fortes.",
        "Sem limitacao critica por quantidade de trades.",
    )
    return out.sort_values(["symbol", "timeframe"]).reset_index(drop=True)


def _asset_timeframe_month_matrix(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["month"] = work["entry_time"].dt.strftime("%Y-%m")
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe, month), chunk in work.groupby(["symbol", "timeframe", "month"], dropna=False):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        rows.append(
            {
                "symbol": str(symbol),
                "timeframe": str(timeframe),
                "month": str(month),
                "trades": int(len(chunk)),
                "profit_factor": _profit_factor_from_pnl(pnl),
                "win_rate": _win_rate_from_pnl(pnl),
                "expectancy": _expectancy_from_pnl(pnl),
                "drawdown": _drawdown_from_pnl(pnl),
                "net_profit": _net_profit_from_pnl(pnl),
                "avg_mfe": float(pd.to_numeric(chunk["mfe"], errors="coerce").mean()),
                "avg_mae": float(pd.to_numeric(chunk["mae"], errors="coerce").mean()),
                "first_trade": chunk["entry_time"].min(),
                "last_trade": chunk["exit_time"].max(),
            }
        )
    return pd.DataFrame(rows).sort_values(["symbol", "timeframe", "month"]).reset_index(drop=True)


def _metric_stability(monthly: pd.DataFrame) -> pd.DataFrame:
    metrics = ["profit_factor", "win_rate", "expectancy", "drawdown"]
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in monthly.groupby(["symbol", "timeframe"], dropna=False):
        row: dict[str, Any] = {
            "symbol": str(symbol),
            "timeframe": str(timeframe),
            "months": int(chunk["month"].nunique()),
            "trades": int(chunk["trades"].sum()),
        }
        for metric in metrics:
            series = pd.to_numeric(chunk[metric], errors="coerce")
            row[f"{metric}_mean"] = float(series.mean()) if not series.empty else np.nan
            row[f"{metric}_median"] = float(series.median()) if not series.empty else np.nan
            row[f"{metric}_std"] = float(series.std(ddof=1)) if len(series) > 1 else 0.0
            row[f"{metric}_cv"] = _cv(series)
            row[f"{metric}_min"] = float(series.min()) if not series.empty else np.nan
            row[f"{metric}_max"] = float(series.max()) if not series.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["profit_factor_mean", "expectancy_mean"], ascending=[False, False]).reset_index(drop=True)


def _rolling_long(df: pd.DataFrame, windows: tuple[int, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rolling_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in df.groupby(["symbol", "timeframe"], dropna=False):
        ordered = chunk.sort_values("entry_time").reset_index(drop=True)
        for window in windows:
            frame = _rolling_metrics(ordered, window)
            if frame.empty:
                summary_rows.append(
                    {
                        "symbol": str(symbol),
                        "timeframe": str(timeframe),
                        "window": window,
                        "observations": 0,
                        "expectancy_slope": np.nan,
                        "profit_factor_slope": np.nan,
                        "persistent_degradation": None,
                    }
                )
                continue
            x = np.arange(len(frame), dtype=float)
            exp_slope = float(np.polyfit(x, frame["expectancy"].to_numpy(dtype=float), 1)[0]) if len(frame) > 1 else 0.0
            pf_slope = float(np.polyfit(x, frame["profit_factor"].to_numpy(dtype=float), 1)[0]) if len(frame) > 1 else 0.0
            first_third = frame.iloc[: max(1, len(frame) // 3)]
            last_third = frame.iloc[-max(1, len(frame) // 3) :]
            persistent_degradation = bool(
                last_third["expectancy"].mean() < first_third["expectancy"].mean()
                and last_third["profit_factor"].mean() < first_third["profit_factor"].mean()
                and exp_slope < 0.0
                and pf_slope < 0.0
            )
            frame["persistent_degradation"] = persistent_degradation
            rolling_frames.append(frame)
            summary_rows.append(
                {
                    "symbol": str(symbol),
                    "timeframe": str(timeframe),
                    "window": window,
                    "observations": int(len(frame)),
                    "expectancy_slope": exp_slope,
                    "profit_factor_slope": pf_slope,
                    "persistent_degradation": persistent_degradation,
                }
            )
    rolling_df = pd.concat(rolling_frames, ignore_index=True) if rolling_frames else pd.DataFrame()
    summary_df = pd.DataFrame(summary_rows).sort_values(["symbol", "timeframe", "window"]).reset_index(drop=True)
    return rolling_df, summary_df


def _persistence(monthly: pd.DataFrame, global_wr: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in monthly.groupby(["symbol", "timeframe"], dropna=False):
        months = max(1, int(chunk["month"].nunique()))
        pf_pos = int((pd.to_numeric(chunk["profit_factor"], errors="coerce") > 1.0).sum())
        exp_pos = int((pd.to_numeric(chunk["expectancy"], errors="coerce") > 0.0).sum())
        wr_pos = int((pd.to_numeric(chunk["win_rate"], errors="coerce") > global_wr).sum())
        rows.append(
            {
                "symbol": str(symbol),
                "timeframe": str(timeframe),
                "positive_pf_months": pf_pos,
                "positive_expectancy_months": exp_pos,
                "above_global_wr_months": wr_pos,
                "total_months": months,
                "persistence_pf_pct": 100.0 * pf_pos / months,
                "persistence_expectancy_pct": 100.0 * exp_pos / months,
                "persistence_wr_pct": 100.0 * wr_pos / months,
                "recurring_edge": bool(pf_pos / months >= 0.6 and exp_pos / months >= 0.6),
            }
        )
    return pd.DataFrame(rows).sort_values(["persistence_pf_pct", "persistence_expectancy_pct"], ascending=[False, False]).reset_index(drop=True)


def _robustness(df: pd.DataFrame, low_threshold: int, high_threshold: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in df.groupby(["symbol", "timeframe"], dropna=False):
        trades = int(len(chunk))
        if trades < low_threshold:
            label = "Baixa robustez"
        elif trades < high_threshold:
            label = "Media robustez"
        else:
            label = "Alta robustez"
        rows.append(
            {
                "symbol": str(symbol),
                "timeframe": str(timeframe),
                "trades": trades,
                "robustness_class": label,
                "can_claim_superiority": bool(trades >= low_threshold),
            }
        )
    return pd.DataFrame(rows).sort_values(["trades", "symbol", "timeframe"], ascending=[False, True, True]).reset_index(drop=True)


def _split_temporal_sensitivity(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in df.groupby(["symbol", "timeframe"], dropna=False):
        ordered = chunk.sort_values("entry_time").reset_index(drop=True)
        blocks = np.array_split(ordered.index.to_numpy(), 3)
        parts: list[dict[str, Any]] = []
        for idx, block in enumerate(blocks, start=1):
            part = ordered.iloc[block]
            pnl = pd.to_numeric(part["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            parts.append(
                {
                    "block": idx,
                    "trades": int(len(part)),
                    "profit_factor": _profit_factor_from_pnl(pnl),
                    "win_rate": _win_rate_from_pnl(pnl),
                    "expectancy": _expectancy_from_pnl(pnl),
                    "drawdown": _drawdown_from_pnl(pnl),
                }
            )
        pf_vals = np.array([p["profit_factor"] for p in parts], dtype=float)
        wr_vals = np.array([p["win_rate"] for p in parts], dtype=float)
        exp_vals = np.array([p["expectancy"] for p in parts], dtype=float)
        pf_score = max(0.0, 1.0 - min(1.0, float(np.std(pf_vals, ddof=0) / max(1.0, abs(np.mean(pf_vals))))))
        wr_score = max(0.0, 1.0 - min(1.0, float(np.std(wr_vals, ddof=0) / max(0.05, abs(np.mean(wr_vals))))))
        exp_scale = max(0.25, abs(float(np.mean(exp_vals))))
        exp_score = max(0.0, 1.0 - min(1.0, float(np.std(exp_vals, ddof=0) / exp_scale)))
        stability = 100.0 * (0.4 * pf_score + 0.3 * wr_score + 0.3 * exp_score)
        abrupt = bool(np.any(np.sign(exp_vals[:-1]) != np.sign(exp_vals[1:]))) if len(exp_vals) > 1 else False
        row: dict[str, Any] = {
            "symbol": str(symbol),
            "timeframe": str(timeframe),
            "temporal_stability_coefficient": stability,
            "abrupt_break": abrupt,
            "edge_change_profile": "ruptura_abrupta" if abrupt else "mudanca_lenta_ou_oscilacao",
        }
        for part in parts:
            block = part.pop("block")
            for key, value in part.items():
                row[f"block_{block}_{key}"] = value
        rows.append(row)
    return pd.DataFrame(rows).sort_values("temporal_stability_coefficient", ascending=False).reset_index(drop=True)


def _bootstrap_combo(df: pd.DataFrame, n_iter: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    for idx, ((symbol, timeframe), chunk) in enumerate(df.groupby(["symbol", "timeframe"], dropna=False)):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        n = pnl.size
        if n == 0:
            continue
        indices = rng.integers(0, n, size=(n_iter, n))
        samples = pnl[indices]
        pf = np.array([_profit_factor_from_pnl(sample) for sample in samples], dtype=float)
        exp = samples.mean(axis=1)
        rows.append(
            {
                "symbol": str(symbol),
                "timeframe": str(timeframe),
                "bootstrap_trades": int(n),
                "pf_bootstrap_mean": float(np.mean(pf)),
                "pf_ci95_low": float(np.quantile(pf, 0.025)),
                "pf_ci95_high": float(np.quantile(pf, 0.975)),
                "pf_prob_gt_1": float(np.mean(pf > 1.0)),
                "expectancy_bootstrap_mean": float(np.mean(exp)),
                "expectancy_ci95_low": float(np.quantile(exp, 0.025)),
                "expectancy_ci95_high": float(np.quantile(exp, 0.975)),
                "expectancy_prob_gt_0": float(np.mean(exp > 0.0)),
            }
        )
    return pd.DataFrame(rows).sort_values(["symbol", "timeframe"]).reset_index(drop=True)


def _reliability_index(
    stability: pd.DataFrame,
    persistence: pd.DataFrame,
    robustness: pd.DataFrame,
    bootstrap: pd.DataFrame,
    rolling_summary: pd.DataFrame,
    monthly: pd.DataFrame,
) -> pd.DataFrame:
    combo_monthly = (
        monthly.groupby(["symbol", "timeframe"], dropna=False)
        .agg(pf_mean=("profit_factor", "mean"), expectancy_mean=("expectancy", "mean"))
        .reset_index()
    )
    combo_monthly["pf_score"] = ((combo_monthly["pf_mean"] - 0.8) / 1.0).clip(0.0, 1.0) * 100.0
    max_abs_exp = max(0.1, float(combo_monthly["expectancy_mean"].abs().max())) if not combo_monthly.empty else 0.1
    combo_monthly["expectancy_score"] = ((combo_monthly["expectancy_mean"] / max_abs_exp).clip(-1.0, 1.0) + 1.0) * 50.0

    rolling_best = (
        rolling_summary[rolling_summary["observations"] > 0]
        .sort_values(["symbol", "timeframe", "window"], ascending=[True, True, False])
        .drop_duplicates(["symbol", "timeframe"])
    )
    rolling_best = rolling_best[["symbol", "timeframe", "persistent_degradation"]].copy()
    rolling_best["rolling_score"] = np.where(rolling_best["persistent_degradation"] == True, 20.0, 80.0)

    out = combo_monthly.merge(persistence, on=["symbol", "timeframe"], how="left")
    out = out.merge(robustness[["symbol", "timeframe", "trades", "robustness_class"]], on=["symbol", "timeframe"], how="left")
    out = out.merge(
        stability[["symbol", "timeframe", "temporal_stability_coefficient"]],
        on=["symbol", "timeframe"],
        how="left",
    )
    out = out.merge(bootstrap, on=["symbol", "timeframe"], how="left")
    out = out.merge(rolling_best[["symbol", "timeframe", "rolling_score"]], on=["symbol", "timeframe"], how="left")

    out["persistence_score"] = (
        out[["persistence_pf_pct", "persistence_expectancy_pct", "persistence_wr_pct"]].mean(axis=1).fillna(0.0)
    )
    out["bootstrap_score"] = 50.0 * out["pf_prob_gt_1"].fillna(0.0) + 50.0 * out["expectancy_prob_gt_0"].fillna(0.0)

    def _ci_score(row: pd.Series) -> float:
        pf_lo = _safe_float(row.get("pf_ci95_low"), default=np.nan)
        exp_lo = _safe_float(row.get("expectancy_ci95_low"), default=np.nan)
        pf_mean = _safe_float(row.get("pf_bootstrap_mean"), default=0.0)
        exp_mean = _safe_float(row.get("expectancy_bootstrap_mean"), default=0.0)
        if pf_lo > 1.0 and exp_lo > 0.0:
            return 100.0
        if pf_lo > 1.0 or exp_lo > 0.0:
            return 75.0
        if pf_mean > 1.0 and exp_mean > 0.0:
            return 45.0
        if pf_mean > 1.0 or exp_mean > 0.0:
            return 30.0
        return 10.0

    out["ci_score"] = out.apply(_ci_score, axis=1)
    out["robustness_score"] = np.select(
        [out["trades"] >= 50, out["trades"] >= 30],
        [100.0, 60.0],
        default=20.0,
    )
    out["rolling_score"] = out["rolling_score"].fillna(30.0)
    out["temporal_stability_score"] = out["temporal_stability_coefficient"].fillna(0.0)
    out["edge_reliability_index"] = (
        0.20 * out["persistence_score"]
        + 0.15 * out["pf_score"]
        + 0.10 * out["expectancy_score"]
        + 0.15 * out["bootstrap_score"]
        + 0.10 * out["ci_score"]
        + 0.15 * out["robustness_score"]
        + 0.10 * out["rolling_score"]
        + 0.05 * out["temporal_stability_score"]
    ).clip(0.0, 100.0)

    def _classify(score: float) -> str:
        if score >= 90.0:
            return "Evidencia Muito Forte"
        if score >= 75.0:
            return "Evidencia Forte"
        if score >= 60.0:
            return "Evidencia Moderada"
        if score >= 40.0:
            return "Evidencia Fraca"
        return "Evidencia Insuficiente"

    out["evidence_class"] = out["edge_reliability_index"].apply(_classify)
    out["superiority_allowed"] = out["trades"] >= 30
    return out.sort_values(["edge_reliability_index", "trades"], ascending=[False, False]).reset_index(drop=True)


def _plot_heatmap(reliability: pd.DataFrame, file_path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
        from matplotlib import colors
    except Exception:
        return False

    if reliability.empty:
        return False

    tfs = sorted(reliability["timeframe"].dropna().astype(str).unique().tolist())
    syms = sorted(reliability["symbol"].dropna().astype(str).unique().tolist())
    tf_idx = {tf: i for i, tf in enumerate(tfs)}
    sym_idx = {sym: i for i, sym in enumerate(syms)}
    max_trades = max(1, int(reliability["trades"].max()))
    norm = colors.Normalize(vmin=min(0.5, float(reliability["pf_mean"].min())), vmax=max(2.5, float(reliability["pf_mean"].max())))
    cmap = plt.get_cmap("RdYlGn")

    fig, ax = plt.subplots(figsize=(max(8, len(tfs) * 1.4), max(4, len(syms) * 0.7)))
    for row in reliability.itertuples(index=False):
        ax.scatter(
            tf_idx[str(row.timeframe)],
            sym_idx[str(row.symbol)],
            s=900,
            marker="s",
            c=[cmap(norm(float(row.pf_mean)))],
            alpha=max(0.15, float(row.trades) / max_trades),
            edgecolors="black",
            linewidths=0.6,
        )
        ax.text(tf_idx[str(row.timeframe)], sym_idx[str(row.symbol)], f"{row.pf_mean:.2f}\nN={row.trades}", ha="center", va="center", fontsize=8)

    ax.set_xticks(range(len(tfs)))
    ax.set_xticklabels(tfs)
    ax.set_yticks(range(len(syms)))
    ax.set_yticklabels(syms)
    ax.set_xlabel("Timeframe")
    ax.set_ylabel("Ativo")
    ax.set_title("Heatmap de Robustez: cor=PF medio mensal, opacidade=numero de trades")
    ax.set_xlim(-0.5, len(tfs) - 0.5)
    ax.set_ylim(-0.5, len(syms) - 0.5)
    ax.invert_yaxis()
    mappable = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    mappable.set_array([])
    fig.colorbar(mappable, ax=ax, label="Profit Factor medio mensal")
    fig.tight_layout()
    fig.savefig(file_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _plot_rolling(rolling_df: pd.DataFrame, file_path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False

    if rolling_df.empty:
        return False

    combos = rolling_df[["symbol", "timeframe"]].drop_duplicates().reset_index(drop=True)
    fig, axes = plt.subplots(len(combos), 1, figsize=(12, max(4, 2.8 * len(combos))), sharex=False)
    if len(combos) == 1:
        axes = [axes]

    for ax, combo in zip(axes, combos.itertuples(index=False)):
        chunk = rolling_df[(rolling_df["symbol"] == combo.symbol) & (rolling_df["timeframe"] == combo.timeframe)]
        for window, part in chunk.groupby("window", dropna=False):
            ordered = part.sort_values("end_time")
            ax.plot(ordered["end_time"], ordered["expectancy"], label=f"Exp {window}")
        ax.axhline(0.0, color="black", linewidth=0.8, linestyle="--")
        ax.set_title(f"{combo.symbol} {combo.timeframe}")
        ax.set_ylabel("Expectancy")
        ax.legend(loc="best", fontsize=8)

    axes[-1].set_xlabel("Tempo")
    fig.tight_layout()
    fig.savefig(file_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _build_report(
    cfg: Config,
    window_table: pd.DataFrame,
    monthly: pd.DataFrame,
    stability: pd.DataFrame,
    persistence: pd.DataFrame,
    robustness: pd.DataFrame,
    split_df: pd.DataFrame,
    reliability: pd.DataFrame,
    rolling_summary: pd.DataFrame,
) -> str:
    global_first = window_table["first_trade"].min() if not window_table.empty else None
    global_last = window_table["last_trade"].max() if not window_table.empty else None
    limited = robustness[robustness["trades"] < cfg.low_robustness_threshold]
    top = reliability.head(5)
    recurring = persistence[persistence["recurring_edge"] == True]
    degraded = rolling_summary[rolling_summary["persistent_degradation"] == True]
    rolling_available = rolling_summary[rolling_summary["observations"] > 0]
    abrupt = split_df[split_df["abrupt_break"] == True]

    lines: list[str] = []
    lines.append("# FASE 14 - Validacao da Estabilidade Temporal do Edge")
    lines.append("")
    lines.append(f"- generated_at_utc: {_now_utc().isoformat()}")
    lines.append(f"- strategy_key: {cfg.strategy_key}")
    lines.append(f"- historical_trade_window: {global_first} -> {global_last}")
    lines.append("")
    lines.append("## 1. Resumo executivo")
    if top.empty:
        lines.append("- Nao foi possivel identificar combinacoes com evidencias suficientes na base disponivel.")
    else:
        best = top.iloc[0]
        lines.append(
            f"- Melhor indice composto: {best['symbol']} {best['timeframe']} com {best['edge_reliability_index']:.1f}/100 ({best['evidence_class']})."
        )
    lines.append(f"- Combinacoes com edge recorrente: {len(recurring)} de {persistence.shape[0]}." if not persistence.empty else "- Sem combinacoes avaliadas.")
    if rolling_available.empty:
        lines.append("- Rolling longo por combinacao ficou indisponivel: nenhuma serie Ativo+Timeframe atingiu 30 trades.")
    else:
        lines.append(f"- Rolling com degradacao persistente: {len(degraded)} ocorrencias resumidas.")
    lines.append(f"- Sensibilidade temporal com ruptura abrupta: {len(abrupt)} combinacoes." if not split_df.empty else "- Split temporal indisponivel.")
    lines.append("")
    lines.append("## 2. Janela historica utilizada")
    if window_table.empty:
        lines.append("- Nenhum trade disponivel em trade_history.")
    else:
        lines.append("- O historico de candles e amplo, mas o historico de trades do ClassicDonchianBreakout disponivel no banco cobre apenas 2026-06-14 a 2026-07-08.")
        lines.append("- Isso limita a avaliacao de estabilidade longa a um recorte curto de trades reais/persistidos.")
    lines.append("")
    lines.append("## 3. Qualidade da amostra")
    lines.append(f"- Combinacoes abaixo de {cfg.low_robustness_threshold} trades: {len(limited)}.")
    lines.append(f"- Combinacoes com robustez alta (>= {cfg.high_robustness_threshold} trades): {int((robustness['trades'] >= cfg.high_robustness_threshold).sum()) if not robustness.empty else 0}.")
    lines.append("")
    lines.append("## 4. Persistencia mensal do edge")
    if recurring.empty:
        lines.append("- Nenhuma combinacao atingiu persistencia recorrente forte no recorte mensal disponivel.")
    else:
        for row in recurring.head(5).itertuples(index=False):
            lines.append(
                f"- {row.symbol} {row.timeframe}: PF>1 em {row.positive_pf_months}/{row.total_months} meses, Expectancy>0 em {row.positive_expectancy_months}/{row.total_months}."
            )
    lines.append("")
    lines.append("## 5. Estabilidade temporal")
    if not stability.empty:
        best_stable = stability.sort_values(["profit_factor_cv", "expectancy_cv"], ascending=[True, True]).head(5)
        for row in best_stable.itertuples(index=False):
            lines.append(
                f"- {row.symbol} {row.timeframe}: PF medio={row.profit_factor_mean:.2f}, PF CV={row.profit_factor_cv:.2f}, Expectancy media={row.expectancy_mean:.4f}."
            )
    lines.append("")
    lines.append("## 6. Indice de Confiabilidade do Edge")
    if not top.empty:
        for row in top.itertuples(index=False):
            lines.append(
                f"- {row.symbol} {row.timeframe}: indice={row.edge_reliability_index:.1f}, classe={row.evidence_class}, trades={row.trades}, superiority_allowed={bool(row.superiority_allowed)}."
            )
    lines.append("")
    lines.append("## 7. Limitacoes estatisticas")
    lines.append("- A limitacao principal esta no historico de trades persistidos da estrategia, nao no historico de candles.")
    lines.append("- A maior parte das combinacoes possui menos de 30 trades, o que impede conclusoes fortes de superioridade.")
    lines.append("- O recorte mensal contem poucos meses civis, entao persistencia mensal deve ser interpretada com cautela.")
    lines.append("")
    lines.append("## 8. Principais conclusoes")
    lines.append("- O edge observado nao pode ser classificado como estavel de longo prazo com o maior rigor estatistico, porque a serie de trades disponivel ainda e curta.")
    lines.append("- E possivel medir heterogeneidade e sinais locais de persistencia, mas nao confirmar estabilidade estrutural longa sem ampliar o historico de trades da propria estrategia.")
    lines.append("- Quando uma combinacao aparece bem posicionada no indice, isso deve ser lido como evidencia observacional condicionada ao recorte atual.")
    lines.append("")
    lines.append("## 9. Recomendacoes para futuras investigacoes")
    lines.append("- Persistir historico mais longo de trades/backtests da estrategia para repetir exatamente a FASE 14 com mais meses observados.")
    lines.append("- Reexecutar a fase periodicamente para verificar se as melhores combinacoes preservam o ranking ao longo do tempo.")
    lines.append("- Comparar os mesmos indicadores em janelas adicionais quando o banco acumular mais trades reais/paper da estrategia.")
    return "\n".join(lines) + "\n"


def run(cfg: Config) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    trades = _load_all_trades(cfg.strategy_key)
    if trades.empty:
        raise RuntimeError("Nenhum trade encontrado para a estrategia informada em trade_history.")

    candle_coverage = _load_candle_coverage(trades)
    enriched = _enrich_all_trades(trades)
    window_table = _historical_window_table(enriched, candle_coverage)
    monthly = _asset_timeframe_month_matrix(enriched)
    stability = _metric_stability(monthly)
    global_wr = _win_rate_from_pnl(pd.to_numeric(enriched["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float))
    persistence = _persistence(monthly, global_wr)
    robustness = _robustness(enriched, cfg.low_robustness_threshold, cfg.high_robustness_threshold)
    split_df = _split_temporal_sensitivity(enriched)
    rolling_df, rolling_summary = _rolling_long(enriched, cfg.rolling_windows)
    bootstrap = _bootstrap_combo(enriched, cfg.bootstrap_iterations, cfg.random_seed)
    reliability = _reliability_index(stability=split_df, persistence=persistence, robustness=robustness, bootstrap=bootstrap, rolling_summary=rolling_summary, monthly=monthly)

    ts = _now_utc().strftime("%Y%m%d_%H%M%S")
    prefix = f"fase14_validacao_estabilidade_temporal_edge_{ts}"

    outputs = {
        "historical_window": results_dir / f"{prefix}_historical_window.csv",
        "asset_timeframe_month_matrix": results_dir / f"{prefix}_asset_timeframe_month_matrix.csv",
        "temporal_stability": results_dir / f"{prefix}_temporal_stability.csv",
        "rolling_windows": results_dir / f"{prefix}_rolling_windows.csv",
        "rolling_summary": results_dir / f"{prefix}_rolling_summary.csv",
        "persistence": results_dir / f"{prefix}_persistence.csv",
        "robustness": results_dir / f"{prefix}_robustness.csv",
        "temporal_split": results_dir / f"{prefix}_temporal_split.csv",
        "bootstrap": results_dir / f"{prefix}_bootstrap.csv",
        "reliability_index": results_dir / f"{prefix}_reliability_index.csv",
        "enriched_trades": results_dir / f"{prefix}_enriched_trades.csv",
        "heatmap": results_dir / f"{prefix}_heatmap.png",
        "rolling_plot": results_dir / f"{prefix}_rolling.png",
        "report_json": results_dir / f"{prefix}.json",
        "report_md": results_dir / f"{prefix}.md",
    }

    window_table.to_csv(outputs["historical_window"], index=False)
    monthly.to_csv(outputs["asset_timeframe_month_matrix"], index=False)
    stability.to_csv(outputs["temporal_stability"], index=False)
    rolling_df.to_csv(outputs["rolling_windows"], index=False)
    rolling_summary.to_csv(outputs["rolling_summary"], index=False)
    persistence.to_csv(outputs["persistence"], index=False)
    robustness.to_csv(outputs["robustness"], index=False)
    split_df.to_csv(outputs["temporal_split"], index=False)
    bootstrap.to_csv(outputs["bootstrap"], index=False)
    reliability.to_csv(outputs["reliability_index"], index=False)
    enriched.to_csv(outputs["enriched_trades"], index=False)

    heatmap_created = _plot_heatmap(reliability, outputs["heatmap"])
    rolling_plot_created = _plot_rolling(rolling_df, outputs["rolling_plot"])
    report_md = _build_report(cfg, window_table, monthly, stability, persistence, robustness, split_df, reliability, rolling_summary)

    payload = {
        "generated_at": _now_utc().isoformat(),
        "strategy_key": cfg.strategy_key,
        "sample_size": int(len(enriched)),
        "global_trade_window": {
            "first_trade": str(window_table["first_trade"].min()) if not window_table.empty else None,
            "last_trade": str(window_table["last_trade"].max()) if not window_table.empty else None,
            "total_trades": int(len(enriched)),
        },
        "limitations": {
            "trade_history_short_window": True,
            "trade_window_start": str(window_table["first_trade"].min()) if not window_table.empty else None,
            "trade_window_end": str(window_table["last_trade"].max()) if not window_table.empty else None,
            "largest_candle_history_available": str(window_table["first_candle"].min()) if not window_table.empty else None,
        },
        "top_reliability": reliability.head(10).to_dict(orient="records"),
        "outputs": {key: str(value) for key, value in outputs.items()},
        "plots_created": {
            "heatmap": heatmap_created,
            "rolling_plot": rolling_plot_created,
        },
    }

    outputs["report_json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["report_md"].write_text(report_md, encoding="utf-8")
    return payload


def main() -> None:
    payload = run(Config())
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()