from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from database.history_models import ScientificTradeSnapshot
from investigacao_cdb_edge_loss import Config as InvestigationConfig
from investigacao_cdb_edge_loss import _add_indicators
from investigacao_cdb_edge_loss import _classify_regimes
from strategies.base_strategy import SignalType


@dataclass(frozen=True)
class ScientificEntrySnapshot:
    signal_market_regime: str | None
    signal_indicator_payload: dict[str, Any]
    warehouse_row: ScientificTradeSnapshot


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default


def _sanitize(value: Any) -> Any:
    if isinstance(value, (np.floating, float)):
        if not math.isfinite(float(value)):
            return None
        return float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def _prepare_scientific_frame(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    work = frame.copy().reset_index().rename(columns={frame.index.name or "index": "open_time"})
    if "open_time" not in work.columns:
        work = work.rename(columns={work.columns[0]: "open_time"})
    work["open_time"] = pd.to_datetime(work["open_time"], utc=True, errors="coerce")
    work["timeframe"] = str(timeframe)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["open_time", "open", "high", "low", "close"]).reset_index(drop=True)
    enriched = _add_indicators(work, InvestigationConfig())
    enriched = _classify_regimes(enriched)
    rel_vol = pd.to_numeric(enriched["relative_volume"], errors="coerce")
    high_q = rel_vol.quantile(0.70)
    low_q = rel_vol.quantile(0.30)
    enriched["volume_regime"] = np.where(
        rel_vol >= high_q,
        "alto_volume",
        np.where(rel_vol <= low_q, "baixo_volume", "volume_medio"),
    )
    return enriched


def _derive_entry_reason(signal: Any, last: pd.Series) -> str | None:
    metadata = getattr(signal, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("reason") is not None:
        return str(metadata.get("reason"))
    signal_type = getattr(signal, "signal", None)
    if signal_type == SignalType.BUY:
        close = _safe_float(last.get("close"))
        upper = _safe_float(last.get("donchian_high_prev"))
        if close is not None and upper is not None and close > upper:
            return "donchian_breakout_up"
        return "buy_signal"
    return None


def build_entry_snapshot(
    *,
    frame: pd.DataFrame,
    symbol: str,
    timeframe: str,
    execution_id: str,
    strategy_name: str,
    strategy_version: str,
    strategy_key: str,
    campaign_id: str | None,
    signal: Any,
    accepted: bool,
    rejection_reason: str | None,
    risk_reward: float | None,
) -> ScientificEntrySnapshot:
    scientific_frame = _prepare_scientific_frame(frame, timeframe)
    last = scientific_frame.iloc[-1]
    entry_reason = _derive_entry_reason(signal, last)
    market_regime = _sanitize(last.get("regime_combo"))
    close = _safe_float(last.get("close"))
    ema_fast = _safe_float(last.get("ema_short"))
    ema_slow = _safe_float(last.get("ema_long"))
    rel_vol = _safe_float(last.get("relative_volume"))
    vol = _safe_float(last.get("volume"))
    indicator_context = {
        "adx": _sanitize(last.get("adx")),
        "atr": _sanitize(last.get("atr")),
        "atr_pct": _sanitize(last.get("atr_pct")),
        "rsi": _sanitize(last.get("rsi")),
        "macd": _sanitize(last.get("macd")),
        "macd_signal": _sanitize(last.get("macd_signal")),
        "macd_hist": _sanitize(last.get("macd_hist")),
        "ema_fast": _sanitize(ema_fast),
        "ema_slow": _sanitize(ema_slow),
        "relative_volume": _sanitize(rel_vol),
        "donchian_upper": _sanitize(last.get("donchian_high_prev")),
        "donchian_lower": _sanitize(last.get("donchian_low_prev")),
        "donchian_width": _sanitize(last.get("donchian_width")),
        "candle_body": _sanitize(last.get("candle_body")),
        "candle_range": _sanitize(last.get("candle_range")),
        "spread": None,
        "distance_breakout": _sanitize(last.get("distance_breakout")),
        "distance_to_ema_fast": _sanitize(None if close in (None, 0.0) or ema_fast is None else (close - ema_fast) / close),
        "distance_to_ema_slow": _sanitize(None if close in (None, 0.0) or ema_slow is None else (close - ema_slow) / close),
        "close": _sanitize(last.get("close")),
        "high": _sanitize(last.get("high")),
        "low": _sanitize(last.get("low")),
        "open": _sanitize(last.get("open")),
        "volume": _sanitize(vol),
        "volume_average": _sanitize(None if rel_vol in (None, 0.0) or vol is None else vol / rel_vol),
    }
    entry_snapshot = {
        "symbol": symbol,
        "timeframe": timeframe,
        "campaign_id": campaign_id,
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "strategy_key": strategy_key,
        "entry_reason": entry_reason,
        "timestamp": _sanitize(last.get("open_time")),
        "accepted": accepted,
        "rejection_reason": rejection_reason,
        "score": _sanitize(getattr(signal, "score", None)),
        "entry_price": _sanitize(getattr(signal, "price", None)),
        "stop_loss": _sanitize(getattr(signal, "stop_loss", None)),
        "take_profit": _sanitize(getattr(signal, "take_profit", None)),
        "risk_reward": _sanitize(risk_reward),
        "market_regime": market_regime,
        "trend_regime": _sanitize(last.get("trend_regime")),
        "volatility_regime": _sanitize(last.get("vol_regime")),
        "volume_regime": _sanitize(last.get("volume_regime")),
        "indicator_context": indicator_context,
    }
    required = {
        "entry_reason": entry_reason,
        "market_regime": market_regime,
        "indicator_context": indicator_context,
    }
    missing_fields = [key for key, value in required.items() if value is None or value == {}]
    warehouse_row = ScientificTradeSnapshot(
        execution_id=execution_id,
        strategy=strategy_key,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        campaign_id=campaign_id,
        symbol=symbol,
        timeframe=timeframe,
        side="BUY",
        entry_timestamp=pd.to_datetime(last.get("open_time"), utc=True).to_pydatetime(),
        entry_reason=entry_reason,
        market_regime=str(market_regime) if market_regime is not None else None,
        trend_regime=str(_sanitize(last.get("trend_regime"))) if _sanitize(last.get("trend_regime")) is not None else None,
        volatility_regime=str(_sanitize(last.get("vol_regime"))) if _sanitize(last.get("vol_regime")) is not None else None,
        volume_regime=str(_sanitize(last.get("volume_regime"))) if _sanitize(last.get("volume_regime")) is not None else None,
        indicator_context_json=json.dumps(indicator_context, ensure_ascii=False),
        entry_snapshot_json=json.dumps(entry_snapshot, ensure_ascii=False),
        snapshot_complete=len(missing_fields) == 0,
        missing_fields_json=json.dumps(missing_fields, ensure_ascii=False),
    )
    indicator_payload = {
        "ema_fast": indicator_context["ema_fast"],
        "ema_slow": indicator_context["ema_slow"],
        "ema_trend": indicator_context["ema_slow"],
        "rsi": indicator_context["rsi"],
        "atr": indicator_context["atr"],
        "volume": indicator_context["volume"],
        "volume_average": indicator_context["volume_average"],
        "close": indicator_context["close"],
        "high": indicator_context["high"],
        "low": indicator_context["low"],
    }
    return ScientificEntrySnapshot(
        signal_market_regime=str(market_regime) if market_regime is not None else None,
        signal_indicator_payload=indicator_payload,
        warehouse_row=warehouse_row,
    )


def build_exit_snapshot(
    *,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    exit_price: float,
    exit_reason: str,
    exit_time: datetime,
    duration_minutes: float,
    pnl: float,
    pnl_pct: float,
    mfe: float | None,
    mae: float | None,
) -> dict[str, Any]:
    risk_per_unit = None if stop_loss is None else max(entry_price - stop_loss, 1e-9)
    realized_rr = None if risk_per_unit is None else (exit_price - entry_price) / risk_per_unit
    return {
        "exit_reason": exit_reason,
        "exit_timestamp": exit_time.isoformat(),
        "holding_time_minutes": duration_minutes,
        "mfe": _sanitize(mfe),
        "mae": _sanitize(mae),
        "return_pct": _sanitize(pnl_pct),
        "pnl": _sanitize(pnl),
        "realized_rr": _sanitize(realized_rr),
        "take_profit": _sanitize(take_profit),
        "stop_loss": _sanitize(stop_loss),
        "exit_price": _sanitize(exit_price),
    }