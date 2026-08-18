"""Shared metric definitions used across the platform.

The goal is to keep every lab and report aligned on the same formulas for
trade-level performance metrics.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _clean_numeric_series(values: pd.Series | list[Any] | tuple[Any, ...]) -> pd.Series:
    series = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0)
    return series.astype(float)


def win_rate_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    if clean.empty:
        return 0.0
    return float((clean > 0).mean())


def gross_profit_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    return float(clean[clean > 0].sum())


def gross_loss_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    return float(abs(clean[clean < 0].sum()))


def profit_factor_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    gross_profit = gross_profit_from_pnl(pnl)
    gross_loss = gross_loss_from_pnl(pnl)
    if gross_loss > 0:
        return float(gross_profit / gross_loss)
    if gross_profit > 0:
        return float("inf")
    return 0.0


def expectancy_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    if clean.empty:
        return 0.0
    return float(clean.mean())


def sharpe_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    if len(clean) < 2:
        return 0.0
    std = float(clean.std(ddof=1))
    if std <= 0:
        return 0.0
    return float(clean.mean() / std * math.sqrt(len(clean)))


def max_drawdown_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    if clean.empty:
        return 0.0
    equity = clean.cumsum()
    peaks = equity.cummax()
    drawdown = peaks - equity
    return float(drawdown.max())


def recovery_factor_from_pnl(pnl: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = _clean_numeric_series(pnl)
    drawdown = max_drawdown_from_pnl(clean)
    net_profit = float(clean.sum())
    if drawdown > 0:
        return float(net_profit / drawdown)
    return float("inf") if net_profit > 0 else 0.0


def capture_ratio_from_realized_and_mfe(realized_return: float, path_mfe: float) -> float:
    if path_mfe > 0:
        return float(realized_return / path_mfe)
    return float("nan")


def mean_capture_ratio(values: pd.Series | list[Any] | tuple[Any, ...]) -> float:
    clean = pd.to_numeric(pd.Series(values), errors="coerce")
    if clean.empty:
        return float("nan")
    return float(clean.mean())


def max_drawdown_from_equity_curve(equity_curve: pd.Series | list[Any] | tuple[Any, ...]) -> tuple[float, float]:
    equity = _clean_numeric_series(equity_curve)
    if equity.empty:
        return 0.0, 0.0
    peaks = equity.cummax()
    drawdown = peaks - equity
    drawdown_pct = drawdown / peaks.replace(0.0, np.nan)
    drawdown_pct = drawdown_pct.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return float(drawdown.max()), float(drawdown_pct.max())