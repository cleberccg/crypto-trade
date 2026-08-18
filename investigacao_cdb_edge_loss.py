from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text

from database.connection import get_session


TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}


@dataclass(frozen=True)
class Config:
    campaign_id: str = "spc-official-cdb-v1"
    strategy_key: str = "ClassicDonchianBreakout@v1.0"
    donchian_window: int = 20
    adx_window: int = 14
    atr_window: int = 14
    rsi_window: int = 14
    bb_window: int = 20
    bb_std: float = 2.0
    ema_short: int = 20
    ema_long: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rel_vol_window: int = 20
    vol_window: int = 20


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _profit_factor(pnl: pd.Series) -> float:
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss <= 0.0:
        return 999.0 if gross_profit > 0.0 else 0.0
    return gross_profit / gross_loss


def _expectancy(pnl: pd.Series) -> float:
    if pnl.empty:
        return 0.0
    return float(pnl.mean())


def _win_rate(pnl: pd.Series) -> float:
    if pnl.empty:
        return 0.0
    return float((pnl > 0).mean())


def _max_drawdown_from_pnl(pnl: pd.Series, initial: float = 10_000.0) -> float:
    if pnl.empty:
        return 0.0
    equity = initial + pnl.cumsum()
    peak = equity.cummax()
    dd = (peak - equity) / peak.replace(0, np.nan)
    dd = dd.fillna(0.0)
    return float(dd.max())


def _load_campaign_execution_ids(base_dir: Path, campaign_id: str) -> list[str]:
    registry = base_dir / "optimization" / "results" / "paper_specialized_campaign_registry.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    campaigns = data.get("campaigns") if isinstance(data.get("campaigns"), dict) else {}
    entry = campaigns.get(campaign_id) if isinstance(campaigns.get(campaign_id), dict) else {}
    values = entry.get("execution_ids") if isinstance(entry.get("execution_ids"), list) else []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if token and token not in seen:
            out.append(token)
            seen.add(token)
    return out


def _load_trades(strategy_key: str, execution_ids: list[str]) -> pd.DataFrame:
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
              AND th.execution_id IN :execution_ids
            ORDER BY th.symbol, th.timeframe, th.entry_time
            """
        ).bindparams(bindparam("execution_ids", expanding=True))
        rows = session.execute(stmt, {"strategy": strategy_key, "execution_ids": execution_ids}).mappings().all()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    dt_cols = ["entry_time", "exit_time"]
    for c in dt_cols:
        df[c] = pd.to_datetime(df[c], utc=True)
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
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["result"] = np.where(df["pnl"] > 0, "WIN", "LOSS")
    df["is_stop"] = df["exit_reason"].fillna("").str.lower().str.contains("stop") | (df["pnl"] <= 0)
    df["is_take"] = df["exit_reason"].fillna("").str.lower().str.contains("take") | (df["pnl"] > 0)
    return df


def _load_candles_for_contexts(trades: pd.DataFrame, lookback_days: int = 45, lookahead_days: int = 2) -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    if trades.empty:
        return out

    grouped = trades.groupby(["symbol", "timeframe"], dropna=False)
    with get_session() as session:
        for (symbol, timeframe), chunk in grouped:
            start = pd.to_datetime(chunk["entry_time"].min(), utc=True).to_pydatetime() - timedelta(days=lookback_days)
            end = pd.to_datetime(chunk["exit_time"].max(), utc=True).to_pydatetime() + timedelta(days=lookahead_days)

            symbols = [str(symbol)]
            if "/" in str(symbol):
                symbols.append(str(symbol).replace("/", ""))

            stmt = text(
                """
                SELECT symbol, timeframe, open_time, open, high, low, close, volume
                FROM candles
                WHERE symbol IN :symbols
                  AND timeframe = :timeframe
                  AND open_time >= :start_dt
                  AND open_time <= :end_dt
                ORDER BY open_time ASC
                """
            ).bindparams(bindparam("symbols", expanding=True))
            rows = session.execute(
                stmt,
                {
                    "symbols": symbols,
                    "timeframe": str(timeframe),
                    "start_dt": start,
                    "end_dt": end,
                },
            ).mappings().all()
            cdf = pd.DataFrame([dict(r) for r in rows])
            if cdf.empty:
                out[(str(symbol), str(timeframe))] = cdf
                continue
            cdf["open_time"] = pd.to_datetime(cdf["open_time"], utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                cdf[col] = pd.to_numeric(cdf[col], errors="coerce")
            cdf = cdf.dropna(subset=["open_time", "open", "high", "low", "close"]).sort_values("open_time").reset_index(drop=True)
            out[(str(symbol), str(timeframe))] = cdf
    return out


def _compute_adx(df: pd.DataFrame, window: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(window, min_periods=max(2, window // 2)).mean()
    plus_di = 100.0 * (plus_dm.rolling(window, min_periods=max(2, window // 2)).sum() / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm.rolling(window, min_periods=max(2, window // 2)).sum() / atr.replace(0, np.nan))
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(window, min_periods=max(2, window // 2)).mean()
    return adx


def _compute_rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    gain = up.rolling(window, min_periods=max(2, window // 2)).mean()
    loss = down.rolling(window, min_periods=max(2, window // 2)).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _add_indicators(cdf: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = cdf.copy()
    close = df["close"]

    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - close.shift(1)).abs(),
            (df["low"] - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = tr.rolling(cfg.atr_window, min_periods=max(2, cfg.atr_window // 2)).mean()
    df["atr_pct"] = df["atr"] / close.replace(0, np.nan)
    df["adx"] = _compute_adx(df, cfg.adx_window)
    df["rsi"] = _compute_rsi(close, cfg.rsi_window)

    df["ema_short"] = close.ewm(span=cfg.ema_short, adjust=False).mean()
    df["ema_long"] = close.ewm(span=cfg.ema_long, adjust=False).mean()
    df["ema_slope"] = df["ema_long"].pct_change(5)

    ema_fast = close.ewm(span=cfg.macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=cfg.macd_slow, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=cfg.macd_signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    vol_avg = df["volume"].rolling(cfg.rel_vol_window, min_periods=max(2, cfg.rel_vol_window // 2)).mean()
    df["relative_volume"] = df["volume"] / vol_avg.replace(0, np.nan)

    up = close.rolling(cfg.bb_window, min_periods=max(2, cfg.bb_window // 2)).mean()
    sd = close.rolling(cfg.bb_window, min_periods=max(2, cfg.bb_window // 2)).std(ddof=0)
    upper = up + cfg.bb_std * sd
    lower = up - cfg.bb_std * sd
    df["bollinger_width"] = (upper - lower) / up.replace(0, np.nan)

    donchian_high_prev = df["high"].rolling(cfg.donchian_window, min_periods=max(2, cfg.donchian_window // 2)).max().shift(1)
    donchian_low_prev = df["low"].rolling(cfg.donchian_window, min_periods=max(2, cfg.donchian_window // 2)).min().shift(1)
    df["donchian_high_prev"] = donchian_high_prev
    df["donchian_low_prev"] = donchian_low_prev
    df["donchian_width"] = (donchian_high_prev - donchian_low_prev) / close.replace(0, np.nan)

    df["distance_breakout"] = (close - donchian_high_prev) / close.replace(0, np.nan)
    df["candle_range"] = (df["high"] - df["low"]) / close.replace(0, np.nan)
    df["candle_body"] = (df["close"] - df["open"]).abs() / close.replace(0, np.nan)
    shadow = (df["high"] - df[["open", "close"]].max(axis=1)) + (df[["open", "close"]].min(axis=1) - df["low"])
    shadow = shadow.abs() / close.replace(0, np.nan)
    df["body_shadow_ratio"] = df["candle_body"] / shadow.replace(0, np.nan)

    ret = close.pct_change()
    df["volatility"] = ret.rolling(cfg.vol_window, min_periods=max(2, cfg.vol_window // 2)).std(ddof=0)

    breakout_up = close > donchian_high_prev
    breakout_dn = close < donchian_low_prev
    df["is_breakout"] = breakout_up | breakout_dn

    ts_last_breakout = df["open_time"].where(df["is_breakout"])
    ts_last_breakout = ts_last_breakout.ffill()
    tf_minutes = TF_MINUTES.get(str(df.get("timeframe", "")).lower(), None)
    if tf_minutes is None:
        tf_minutes = 5
    delta_minutes = (df["open_time"] - ts_last_breakout).dt.total_seconds() / 60.0
    df["candles_since_last_breakout"] = delta_minutes / max(1, tf_minutes)

    return df


def _attach_entry_features(trades: pd.DataFrame, candles_by_ctx: dict[tuple[str, str], pd.DataFrame], cfg: Config) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for (symbol, timeframe), chunk in trades.groupby(["symbol", "timeframe"], dropna=False):
        key = (str(symbol), str(timeframe))
        cdf = candles_by_ctx.get(key)
        if cdf is None or cdf.empty:
            temp = chunk.copy()
            frames.append(temp)
            continue

        work = _add_indicators(cdf.assign(timeframe=str(timeframe)), cfg)
        left = chunk.sort_values("entry_time").copy()
        right = work.sort_values("open_time").copy()

        merged = pd.merge_asof(
            left,
            right,
            left_on="entry_time",
            right_on="open_time",
            direction="backward",
            tolerance=pd.Timedelta("3D"),
        )

        # Keep trade identity columns stable after asof merge.
        if "symbol_x" in merged.columns:
            merged["symbol"] = merged["symbol_x"]
        if "timeframe_x" in merged.columns:
            merged["timeframe"] = merged["timeframe_x"]
        drop_cols = [c for c in ["symbol_x", "symbol_y", "timeframe_x", "timeframe_y"] if c in merged.columns]
        if drop_cols:
            merged = merged.drop(columns=drop_cols)

        merged["distance_to_ema"] = (merged["entry_price"] - merged["ema_long"]) / merged["entry_price"].replace(0, np.nan)
        frames.append(merged)

    out = pd.concat(frames, ignore_index=True) if frames else trades.copy()
    return out


def _trade_path_metrics(trades: pd.DataFrame, candles_by_ctx: dict[tuple[str, str], pd.DataFrame], cfg: Config) -> pd.DataFrame:
    records: list[dict[str, Any]] = []

    for _, row in trades.iterrows():
        symbol = str(row["symbol"])
        timeframe = str(row["timeframe"])
        key = (symbol, timeframe)
        cdf = candles_by_ctx.get(key)

        record = {
            "id": int(row["id"]),
            "mfe": np.nan,
            "mae": np.nan,
            "max_advance_after_breakout": np.nan,
            "candles_above_channel": np.nan,
            "candles_until_return_inside": np.nan,
            "fail_1": False,
            "fail_2": False,
            "fail_3": False,
            "fail_5": False,
            "near_tp": False,
            "died_immediately": False,
            "stop_after_positive": False,
            "time_to_stop_min": np.nan,
            "time_to_take_min": np.nan,
            "time_since_prev_breakout_trade_candles": np.nan,
            "false_breakout_consecutive_before": np.nan,
        }

        if cdf is None or cdf.empty:
            records.append(record)
            continue

        entry_t = pd.to_datetime(row["entry_time"], utc=True)
        exit_t = pd.to_datetime(row["exit_time"], utc=True)
        entry_price = _safe_float(row["entry_price"], default=np.nan)
        if np.isnan(entry_price):
            records.append(record)
            continue

        seg = cdf[(cdf["open_time"] >= entry_t) & (cdf["open_time"] <= exit_t)].copy()
        if seg.empty:
            records.append(record)
            continue

        side = str(row.get("side") or "BUY").upper()
        side_sign = 1.0 if side == "BUY" else -1.0

        favorable = ((seg["high"] - entry_price) / entry_price) * side_sign
        adverse = ((seg["low"] - entry_price) / entry_price) * side_sign
        record["mfe"] = float(favorable.max())
        record["mae"] = float(adverse.min())

        upper_prev = _safe_float(row.get("donchian_high_prev"), default=np.nan)
        lower_prev = _safe_float(row.get("donchian_low_prev"), default=np.nan)
        if not np.isnan(upper_prev) and not np.isnan(lower_prev):
            if side_sign > 0:
                above = seg["close"] > upper_prev
                inside = seg["close"] <= upper_prev
                advance = (seg["high"] - upper_prev) / entry_price
            else:
                above = seg["close"] < lower_prev
                inside = seg["close"] >= lower_prev
                advance = (lower_prev - seg["low"]) / entry_price

            c_above = 0
            for v in above.tolist():
                if bool(v):
                    c_above += 1
                else:
                    break
            record["candles_above_channel"] = float(c_above)

            idx_inside = np.where(inside.to_numpy())[0]
            if len(idx_inside) > 0:
                c_return = int(idx_inside[0]) + 1
                record["candles_until_return_inside"] = float(c_return)
                record["fail_1"] = c_return <= 1
                record["fail_2"] = c_return <= 2
                record["fail_3"] = c_return <= 3
                record["fail_5"] = c_return <= 5
            else:
                record["candles_until_return_inside"] = float(len(seg))

            first_return_idx = int(idx_inside[0]) if len(idx_inside) > 0 else len(seg)
            if first_return_idx <= 0:
                max_adv = float(advance.iloc[0]) if not advance.empty else 0.0
            else:
                max_adv = float(advance.iloc[:first_return_idx].max()) if first_return_idx > 0 else 0.0
            record["max_advance_after_breakout"] = max_adv

        take_profit = _safe_float(row.get("take_profit"), default=np.nan)
        if not np.isnan(take_profit):
            if side_sign > 0:
                best_high = float(seg["high"].max())
                record["near_tp"] = best_high >= (entry_price + 0.8 * (take_profit - entry_price))
            else:
                best_low = float(seg["low"].min())
                record["near_tp"] = best_low <= (entry_price - 0.8 * (entry_price - take_profit))

        tf_min = TF_MINUTES.get(timeframe.lower(), 5)
        record["died_immediately"] = len(seg) <= max(1, int(3 * (5 / max(1, tf_min))))

        pnl = _safe_float(row.get("pnl"), default=0.0)
        is_stop = bool(str(row.get("exit_reason") or "").lower().find("stop") >= 0 or pnl <= 0)
        if is_stop:
            record["stop_after_positive"] = record["mfe"] > 0
            dur = _safe_float(row.get("duration_minutes"), default=np.nan)
            record["time_to_stop_min"] = dur
        else:
            dur = _safe_float(row.get("duration_minutes"), default=np.nan)
            record["time_to_take_min"] = dur

        records.append(record)

    extra = pd.DataFrame(records)
    return trades.merge(extra, on="id", how="left")


def _stage1_trade_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = out["entry_time"].dt.strftime("%Y-%m-%d")
    out["time"] = out["entry_time"].dt.strftime("%H:%M:%S")

    cols = [
        "id",
        "symbol",
        "timeframe",
        "date",
        "time",
        "entry_price",
        "result",
        "pnl",
        "pnl_percent",
        "exit_reason",
        "adx",
        "atr",
        "atr_pct",
        "rsi",
        "macd",
        "macd_hist",
        "ema_short",
        "ema_long",
        "distance_to_ema",
        "volume",
        "relative_volume",
        "donchian_width",
        "bollinger_width",
        "volatility",
        "distance_breakout",
        "candle_range",
        "candle_body",
        "body_shadow_ratio",
        "mfe",
        "mae",
        "max_advance_after_breakout",
        "candles_above_channel",
        "candles_until_return_inside",
    ]

    keep = [c for c in cols if c in out.columns]
    return out[keep].sort_values(["symbol", "date", "time"]).reset_index(drop=True)


def _summary_stats_by_result(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    winners = df[df["result"] == "WIN"]
    losers = df[df["result"] == "LOSS"]
    for col in features:
        if col not in df.columns:
            continue
        w = pd.to_numeric(winners[col], errors="coerce").dropna()
        l = pd.to_numeric(losers[col], errors="coerce").dropna()
        pooled_std = float(np.sqrt((w.var(ddof=0) if len(w) > 0 else 0.0) + (l.var(ddof=0) if len(l) > 0 else 0.0)))
        diff = float(w.mean() - l.mean()) if len(w) and len(l) else np.nan
        effect = abs(diff) / pooled_std if pooled_std > 1e-12 else np.nan
        rows.append(
            {
                "feature": col,
                "win_mean": float(w.mean()) if len(w) else np.nan,
                "win_median": float(w.median()) if len(w) else np.nan,
                "win_std": float(w.std(ddof=0)) if len(w) else np.nan,
                "loss_mean": float(l.mean()) if len(l) else np.nan,
                "loss_median": float(l.median()) if len(l) else np.nan,
                "loss_std": float(l.std(ddof=0)) if len(l) else np.nan,
                "mean_diff": diff,
                "effect_size_abs": effect,
                "n_win": int(len(w)),
                "n_loss": int(len(l)),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("effect_size_abs", ascending=False, na_position="last").reset_index(drop=True)


def _asset_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, chunk in df.groupby("symbol", dropna=False):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0)
        rows.append(
            {
                "symbol": symbol,
                "trades": int(len(chunk)),
                "profit_factor": _profit_factor(pnl),
                "expectancy": _expectancy(pnl),
                "win_rate": _win_rate(pnl),
                "avg_mfe": float(pd.to_numeric(chunk["mfe"], errors="coerce").mean()),
                "avg_mae": float(pd.to_numeric(chunk["mae"], errors="coerce").mean()),
                "avg_duration_min": float(pd.to_numeric(chunk["duration_minutes"], errors="coerce").mean()),
                "avg_time_to_stop_min": float(pd.to_numeric(chunk["time_to_stop_min"], errors="coerce").mean()),
                "avg_time_to_take_min": float(pd.to_numeric(chunk["time_to_take_min"], errors="coerce").mean()),
                "drawdown": _max_drawdown_from_pnl(pnl),
            }
        )
    return pd.DataFrame(rows).sort_values("profit_factor", ascending=False).reset_index(drop=True)


def _window_metrics(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for days in [7, 14, 30]:
        start = now - timedelta(days=days)
        chunk = df[df["exit_time"] >= start]
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0)
        rows.append(
            {
                "window_days": days,
                "trades": int(len(chunk)),
                "profit_factor": _profit_factor(pnl),
                "expectancy": _expectancy(pnl),
                "win_rate": _win_rate(pnl),
                "net_pnl": float(pnl.sum()),
            }
        )
    return pd.DataFrame(rows)


def _classify_regimes(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    adx = pd.to_numeric(out["adx"], errors="coerce")
    vol = pd.to_numeric(out["volatility"], errors="coerce")
    bbw = pd.to_numeric(out["bollinger_width"], errors="coerce")
    slope = pd.to_numeric(out["ema_slope"], errors="coerce")

    adx_strong = adx.quantile(0.70)
    adx_weak = adx.quantile(0.40)
    vol_high = vol.quantile(0.70)
    vol_low = vol.quantile(0.30)
    bbw_high = bbw.quantile(0.70)
    bbw_low = bbw.quantile(0.30)

    trend = np.where(adx >= adx_strong, "tendencia_forte", np.where(adx <= adx_weak, "lateral", "tendencia_fraca"))
    # Use ema slope sign to split weak/strong direction when available.
    trend = np.where((trend == "tendencia_forte") & (slope < 0), "tendencia_forte_baixa", trend)
    trend = np.where((trend == "tendencia_forte") & (slope >= 0), "tendencia_forte_alta", trend)

    vol_reg = np.where(vol >= vol_high, "alta_volatilidade", np.where(vol <= vol_low, "baixa_volatilidade", "vol_media"))
    comp = np.where(bbw <= bbw_low, "compressao", np.where(bbw >= bbw_high, "expansao", "neutro"))

    out["trend_regime"] = trend
    out["vol_regime"] = vol_reg
    out["compression_regime"] = comp
    out["regime_combo"] = out["trend_regime"].astype(str) + "|" + out["vol_regime"].astype(str) + "|" + out["compression_regime"].astype(str)
    return out


def _pf_by_group(df: pd.DataFrame, col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for k, chunk in df.groupby(col, dropna=False):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0)
        rows.append(
            {
                col: k,
                "trades": int(len(chunk)),
                "profit_factor": _profit_factor(pnl),
                "expectancy": _expectancy(pnl),
                "win_rate": _win_rate(pnl),
                "net_pnl": float(pnl.sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("profit_factor", ascending=False).reset_index(drop=True)


def _donchian_trade_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values(["symbol", "entry_time"]).copy()

    out["breakout_trade"] = pd.to_numeric(out["distance_breakout"], errors="coerce") > 0
    out["false_breakout"] = out["breakout_trade"] & (out["result"] == "LOSS") & (
        pd.to_numeric(out["candles_until_return_inside"], errors="coerce") <= 3
    )

    out["false_breakout_consecutive_before"] = 0
    out["time_since_prev_breakout_trade_candles"] = np.nan

    for symbol, chunk in out.groupby("symbol", sort=False):
        idx = chunk.index.tolist()
        prev_breakout_time: pd.Timestamp | None = None
        consec_false = 0
        tf_min = TF_MINUTES.get(str(chunk["timeframe"].iloc[0]).lower(), 5)

        for i in idx:
            is_break = bool(out.at[i, "breakout_trade"])
            entry_t = out.at[i, "entry_time"]
            if is_break and prev_breakout_time is not None:
                delta_c = (entry_t - prev_breakout_time).total_seconds() / 60.0 / max(1, tf_min)
                out.at[i, "time_since_prev_breakout_trade_candles"] = delta_c
            out.at[i, "false_breakout_consecutive_before"] = consec_false

            if is_break:
                prev_breakout_time = entry_t
                if bool(out.at[i, "false_breakout"]):
                    consec_false += 1
                else:
                    consec_false = 0

    return out


def _filter_what_if(df: pd.DataFrame, feature: str, min_keep_ratio: float = 0.0) -> dict[str, Any]:
    work = df.copy()
    vals = pd.to_numeric(work[feature], errors="coerce")
    work = work.assign(_x=vals)
    work = work.dropna(subset=["_x"])
    if work.empty:
        return {
            "feature": feature,
            "rule": None,
            "threshold": None,
            "kept_trades": 0,
            "kept_ratio": 0.0,
            "avoided_stops": 0,
            "lost_takes": 0,
            "net_pnl_delta": 0.0,
        }

    base_pnl = float(pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0).sum())
    q = np.linspace(0.1, 0.9, 17)
    thresholds = sorted({float(work["_x"].quantile(v)) for v in q})

    best: dict[str, Any] | None = None
    n_total = len(df)
    is_stop_all = df["is_stop"].astype(bool)
    is_take_all = (df["result"] == "WIN")

    for thr in thresholds:
        for rule in ["ge", "le"]:
            if rule == "ge":
                mask = vals >= thr
            else:
                mask = vals <= thr
            kept = df[mask.fillna(False)].copy()
            dropped = df[~mask.fillna(False)].copy()

            kept_ratio = len(kept) / max(1, n_total)
            if kept_ratio < min_keep_ratio:
                continue

            kept_pnl = float(pd.to_numeric(kept["pnl"], errors="coerce").fillna(0.0).sum())
            delta = kept_pnl - base_pnl
            avoided_stops = int(dropped["is_stop"].astype(bool).sum())
            lost_takes = int((dropped["result"] == "WIN").sum())

            row = {
                "feature": feature,
                "rule": rule,
                "threshold": thr,
                "kept_trades": int(len(kept)),
                "kept_ratio": kept_ratio,
                "avoided_stops": avoided_stops,
                "lost_takes": lost_takes,
                "net_pnl_delta": delta,
            }

            if best is None:
                best = row
            else:
                if row["net_pnl_delta"] > best["net_pnl_delta"]:
                    best = row
                elif row["net_pnl_delta"] == best["net_pnl_delta"] and row["kept_ratio"] > best["kept_ratio"]:
                    best = row

    if best is None:
        return {
            "feature": feature,
            "rule": None,
            "threshold": None,
            "kept_trades": 0,
            "kept_ratio": 0.0,
            "avoided_stops": 0,
            "lost_takes": 0,
            "net_pnl_delta": 0.0,
        }
    return best


def _variable_importance(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    work = df.copy()
    work["target"] = (work["result"] == "WIN").astype(int)
    X = work[features].apply(pd.to_numeric, errors="coerce")
    X = X.replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median(numeric_only=True))
    y = work["target"].astype(int)

    try:
        from sklearn.ensemble import RandomForestClassifier

        model = RandomForestClassifier(
            n_estimators=400,
            max_depth=5,
            min_samples_leaf=5,
            random_state=42,
            class_weight="balanced",
        )
        model.fit(X, y)
        imp = model.feature_importances_
        out = pd.DataFrame({"feature": features, "importance": imp, "method": "random_forest"})
        return out.sort_values("importance", ascending=False).reset_index(drop=True)
    except Exception:
        rows: list[dict[str, Any]] = []
        winners = work[work["target"] == 1]
        losers = work[work["target"] == 0]
        for f in features:
            w = pd.to_numeric(winners[f], errors="coerce").dropna()
            l = pd.to_numeric(losers[f], errors="coerce").dropna()
            if len(w) == 0 or len(l) == 0:
                score = 0.0
            else:
                pooled = float(np.sqrt((w.var(ddof=0) + l.var(ddof=0))))
                score = abs(float(w.mean() - l.mean())) / pooled if pooled > 1e-12 else 0.0
            rows.append({"feature": f, "importance": score, "method": "effect_size_fallback"})
        return pd.DataFrame(rows).sort_values("importance", ascending=False).reset_index(drop=True)


def _build_final_answers(
    df: pd.DataFrame,
    stats: pd.DataFrame,
    asset: pd.DataFrame,
    regime_pf: pd.DataFrame,
    filters_balanced: pd.DataFrame,
) -> dict[str, Any]:
    recent30 = df[df["exit_time"] >= (_now_utc() - timedelta(days=30))]
    older = df[df["exit_time"] < (_now_utc() - timedelta(days=30))]
    pf_recent = _profit_factor(pd.to_numeric(recent30["pnl"], errors="coerce").fillna(0.0)) if not recent30.empty else np.nan
    pf_older = _profit_factor(pd.to_numeric(older["pnl"], errors="coerce").fillna(0.0)) if not older.empty else np.nan

    market_vs_strategy = "mudanca_de_mercado_predominante"
    if np.isfinite(pf_recent) and np.isfinite(pf_older):
        if pf_recent < pf_older:
            market_vs_strategy = "mudanca_de_mercado_com_limitacao_estrategica"
        else:
            market_vs_strategy = "limitacao_estrategica_predominante"

    top_diffs = stats.head(8)[["feature", "win_mean", "loss_mean", "effect_size_abs"]].to_dict(orient="records")

    false_break_pattern = {
        "fail_1_pct": float(df["fail_1"].mean() * 100.0),
        "fail_2_pct": float(df["fail_2"].mean() * 100.0),
        "fail_3_pct": float(df["fail_3"].mean() * 100.0),
        "fail_5_pct": float(df["fail_5"].mean() * 100.0),
    }

    edge_assets = asset[asset["profit_factor"] > 1.0]["symbol"].tolist()
    no_edge_assets = asset[asset["profit_factor"] <= 1.0]["symbol"].tolist()

    general_or_concentrated = "concentrado_em_ativos_especificos"
    if len(edge_assets) == 0:
        general_or_concentrated = "geral"

    best_balanced = filters_balanced.sort_values(["net_pnl_delta", "kept_ratio"], ascending=[False, False]).head(1)
    best_balanced_row = best_balanced.to_dict(orient="records")[0] if not best_balanced.empty else {}

    return {
        "q1_motivo_perda": market_vs_strategy,
        "q2_variaveis_diferenciam_win_loss": top_diffs,
        "q3_padrao_falso_rompimento": false_break_pattern,
        "q4_ativos_com_edge": edge_assets,
        "q5_ativos_sem_edge": no_edge_assets,
        "q6_problema_geral_ou_concentrado": general_or_concentrated,
        "q7_filtro_maior_impacto_sem_reduzir_muito": best_balanced_row,
        "q8_melhor_hipotese_recuperacao": (
            "foco_em_evitar_falsos_rompimentos_rapidos_pos_breakout, priorizando contextos com "
            "maior adx, maior volume relativo e menor sinal de compressao extrema"
        ),
    }


def _markdown_report(
    cfg: Config,
    trades_df: pd.DataFrame,
    stage2_stats: pd.DataFrame,
    stage3_breakout: dict[str, Any],
    stage4_mfe_mae: dict[str, Any],
    stage5_asset: pd.DataFrame,
    stage6_windows: pd.DataFrame,
    stage7_regimes: dict[str, pd.DataFrame],
    stage8_donchian: dict[str, Any],
    stage9_filters: pd.DataFrame,
    stage9_filters_balanced: pd.DataFrame,
    stage10_importance: pd.DataFrame,
    final_answers: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# Investigacao Cientifica - Perda de Edge ClassicDonchianBreakout")
    lines.append("")
    lines.append(f"- generated_at_utc: {_now_utc().isoformat()}")
    lines.append(f"- campaign_id: {cfg.campaign_id}")
    lines.append(f"- strategy: {cfg.strategy_key}")
    lines.append(f"- sample_trades: {len(trades_df)}")
    lines.append("")

    lines.append("## Etapa 2 - Diferencas entre WIN e LOSS")
    for _, r in stage2_stats.head(12).iterrows():
        lines.append(
            "- "
            f"{r['feature']}: "
            f"win_mean={_safe_float(r['win_mean']):.6f}, "
            f"loss_mean={_safe_float(r['loss_mean']):.6f}, "
            f"effect={_safe_float(r['effect_size_abs']):.4f}"
        )
    lines.append("")

    lines.append("## Etapa 3 - Qualidade do Rompimento")
    for k, v in stage3_breakout.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## Etapa 4 - MFE/MAE")
    for k, v in stage4_mfe_mae.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## Etapa 5 - Analise por Ativo")
    for _, r in stage5_asset.iterrows():
        lines.append(
            "- "
            f"{r['symbol']}: PF={_safe_float(r['profit_factor']):.4f}, "
            f"Expectancy={_safe_float(r['expectancy']):.4f}, "
            f"WinRate={_safe_float(r['win_rate'])*100.0:.2f}%, "
            f"MFE={_safe_float(r['avg_mfe']):.4f}, MAE={_safe_float(r['avg_mae']):.4f}, "
            f"DD={_safe_float(r['drawdown']):.4f}"
        )
    lines.append("")

    lines.append("## Etapa 6 - Analise Temporal")
    for _, r in stage6_windows.iterrows():
        lines.append(
            "- "
            f"last_{int(r['window_days'])}d: trades={int(r['trades'])}, "
            f"PF={_safe_float(r['profit_factor']):.4f}, "
            f"Expectancy={_safe_float(r['expectancy']):.4f}, "
            f"WinRate={_safe_float(r['win_rate'])*100.0:.2f}%, "
            f"NetPnL={_safe_float(r['net_pnl']):.4f}"
        )
    lines.append("")

    lines.append("## Etapa 7 - Regime de Mercado")
    for name, table in stage7_regimes.items():
        lines.append(f"- {name} top:")
        for _, r in table.head(5).iterrows():
            key_col = [c for c in table.columns if c not in {"trades", "profit_factor", "expectancy", "win_rate", "net_pnl"}][0]
            lines.append(
                "  "
                f"{r[key_col]} | trades={int(r['trades'])} PF={_safe_float(r['profit_factor']):.4f} "
                f"WR={_safe_float(r['win_rate'])*100.0:.2f}%"
            )
    lines.append("")

    lines.append("## Etapa 8 - Donchian")
    for k, v in stage8_donchian.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## Etapa 9 - Filtros Hipoteticos")
    lines.append("- Melhor ganho liquido bruto:")
    for _, r in stage9_filters.head(5).iterrows():
        lines.append(
            "  "
            f"{r['feature']} {r['rule']} {r['threshold']:.6f} | kept={int(r['kept_trades'])} "
            f"avoided_stops={int(r['avoided_stops'])} lost_takes={int(r['lost_takes'])} "
            f"delta_pnl={_safe_float(r['net_pnl_delta']):.4f}"
        )
    lines.append("- Melhor com restricao de manter >=70% trades:")
    for _, r in stage9_filters_balanced.head(5).iterrows():
        lines.append(
            "  "
            f"{r['feature']} {r['rule']} {r['threshold']:.6f} | kept_ratio={_safe_float(r['kept_ratio'])*100.0:.2f}% "
            f"avoided_stops={int(r['avoided_stops'])} lost_takes={int(r['lost_takes'])} "
            f"delta_pnl={_safe_float(r['net_pnl_delta']):.4f}"
        )
    lines.append("")

    lines.append("## Etapa 10 - Importancia de Variaveis")
    for _, r in stage10_importance.head(12).iterrows():
        lines.append(f"- {r['feature']}: {_safe_float(r['importance']):.6f} ({r['method']})")
    lines.append("")

    lines.append("## Etapa 12 - Respostas Objetivas")
    lines.append(f"1) perda por mercado ou estrategia: {final_answers['q1_motivo_perda']}")
    lines.append("2) variaveis que mais diferenciam win/loss:")
    for row in final_answers["q2_variaveis_diferenciam_win_loss"][:8]:
        lines.append(
            "   "
            f"{row['feature']} | win={_safe_float(row['win_mean']):.6f} "
            f"loss={_safe_float(row['loss_mean']):.6f} effect={_safe_float(row['effect_size_abs']):.4f}"
        )
    lines.append(f"3) padrao de falsos rompimentos: {final_answers['q3_padrao_falso_rompimento']}")
    lines.append(f"4) ativos com edge: {final_answers['q4_ativos_com_edge']}")
    lines.append(f"5) ativos sem edge: {final_answers['q5_ativos_sem_edge']}")
    lines.append(f"6) problema geral ou concentrado: {final_answers['q6_problema_geral_ou_concentrado']}")
    lines.append(f"7) melhor filtro equilibrado: {final_answers['q7_filtro_maior_impacto_sem_reduzir_muito']}")
    lines.append(f"8) hipotese de recuperacao: {final_answers['q8_melhor_hipotese_recuperacao']}")

    return "\n".join(lines) + "\n"


def run(cfg: Config) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    execution_ids = _load_campaign_execution_ids(base_dir, cfg.campaign_id)
    if not execution_ids:
        raise RuntimeError(f"Nenhum execution_id encontrado para campaign_id={cfg.campaign_id}")

    trades = _load_trades(cfg.strategy_key, execution_ids)
    if trades.empty:
        raise RuntimeError("Nenhum trade encontrado em trade_history para a campanha informada.")

    candles_by_ctx = _load_candles_for_contexts(trades)
    enriched = _attach_entry_features(trades, candles_by_ctx, cfg)
    enriched = _trade_path_metrics(enriched, candles_by_ctx, cfg)
    enriched = _donchian_trade_diagnostics(enriched)
    enriched = _classify_regimes(enriched)

    stage1_table = _stage1_trade_table(enriched)

    features = [
        "adx",
        "atr",
        "atr_pct",
        "rsi",
        "macd",
        "macd_hist",
        "ema_short",
        "ema_long",
        "distance_to_ema",
        "volume",
        "relative_volume",
        "donchian_width",
        "bollinger_width",
        "volatility",
        "distance_breakout",
        "candle_range",
        "candle_body",
        "body_shadow_ratio",
        "mfe",
        "mae",
        "ema_slope",
        "candles_since_last_breakout",
    ]
    features = [f for f in features if f in enriched.columns]

    stage2_stats = _summary_stats_by_result(enriched, features)

    stage3_breakout = {
        "avg_advance_after_breakout": float(pd.to_numeric(enriched["max_advance_after_breakout"], errors="coerce").mean()),
        "avg_candles_above_channel": float(pd.to_numeric(enriched["candles_above_channel"], errors="coerce").mean()),
        "avg_candles_until_return_inside": float(pd.to_numeric(enriched["candles_until_return_inside"], errors="coerce").mean()),
        "fail_1_candle_pct": float(enriched["fail_1"].mean() * 100.0),
        "fail_2_candles_pct": float(enriched["fail_2"].mean() * 100.0),
        "fail_3_candles_pct": float(enriched["fail_3"].mean() * 100.0),
        "fail_5_candles_pct": float(enriched["fail_5"].mean() * 100.0),
    }

    stage4_mfe_mae = {
        "avg_mfe": float(pd.to_numeric(enriched["mfe"], errors="coerce").mean()),
        "avg_mae": float(pd.to_numeric(enriched["mae"], errors="coerce").mean()),
        "stops_after_positive_count": int((enriched["stop_after_positive"] == True).sum()),
        "near_tp_count": int((enriched["near_tp"] == True).sum()),
        "died_immediately_count": int((enriched["died_immediately"] == True).sum()),
    }

    stage5_asset = _asset_metrics(enriched)
    stage6_windows = _window_metrics(enriched, _now_utc())

    stage7_regimes = {
        "trend_regime": _pf_by_group(enriched, "trend_regime"),
        "vol_regime": _pf_by_group(enriched, "vol_regime"),
        "compression_regime": _pf_by_group(enriched, "compression_regime"),
        "regime_combo": _pf_by_group(enriched, "regime_combo"),
    }

    stage8_donchian = {
        "avg_donchian_width": float(pd.to_numeric(enriched["donchian_width"], errors="coerce").mean()),
        "avg_time_since_last_breakout_candles": float(pd.to_numeric(enriched["candles_since_last_breakout"], errors="coerce").mean()),
        "avg_false_breakout_consecutive_before": float(pd.to_numeric(enriched["false_breakout_consecutive_before"], errors="coerce").mean()),
        "avg_distance_between_breakouts_candles": float(pd.to_numeric(enriched["time_since_prev_breakout_trade_candles"], errors="coerce").mean()),
    }

    filter_features = [
        "adx",
        "relative_volume",
        "atr_pct",
        "bollinger_width",
        "rsi",
        "ema_slope",
        "distance_breakout",
        "mfe",
    ]
    filter_features = [f for f in filter_features if f in enriched.columns]

    stage9_rows = [_filter_what_if(enriched, f, min_keep_ratio=0.0) for f in filter_features]
    stage9_rows_balanced = [_filter_what_if(enriched, f, min_keep_ratio=0.70) for f in filter_features]
    stage9_filters = pd.DataFrame(stage9_rows).sort_values(["net_pnl_delta", "kept_ratio"], ascending=[False, False]).reset_index(drop=True)
    stage9_filters_balanced = pd.DataFrame(stage9_rows_balanced).sort_values(["net_pnl_delta", "kept_ratio"], ascending=[False, False]).reset_index(drop=True)

    stage10_importance = _variable_importance(enriched, [f for f in features if f in enriched.columns and f not in {"mfe", "mae"}])

    final_answers = _build_final_answers(
        enriched,
        stage2_stats,
        stage5_asset,
        stage7_regimes["regime_combo"],
        stage9_filters_balanced,
    )

    ts = _now_utc().strftime("%Y%m%d_%H%M%S")
    prefix = f"investigacao_cdb_edge_loss_{ts}"

    stage1_csv = results_dir / f"{prefix}_stage1_trade_table.csv"
    stage2_csv = results_dir / f"{prefix}_stage2_win_loss_stats.csv"
    stage5_csv = results_dir / f"{prefix}_stage5_asset_metrics.csv"
    stage6_csv = results_dir / f"{prefix}_stage6_temporal_windows.csv"
    stage7_trend_csv = results_dir / f"{prefix}_stage7_trend_regime.csv"
    stage7_vol_csv = results_dir / f"{prefix}_stage7_vol_regime.csv"
    stage7_comp_csv = results_dir / f"{prefix}_stage7_compression_regime.csv"
    stage7_combo_csv = results_dir / f"{prefix}_stage7_regime_combo.csv"
    stage9_csv = results_dir / f"{prefix}_stage9_filters.csv"
    stage9_bal_csv = results_dir / f"{prefix}_stage9_filters_balanced.csv"
    stage10_csv = results_dir / f"{prefix}_stage10_importance.csv"
    enriched_csv = results_dir / f"{prefix}_enriched_trades.csv"
    report_json = results_dir / f"{prefix}.json"
    report_md = results_dir / f"{prefix}.md"

    stage1_table.to_csv(stage1_csv, index=False)
    stage2_stats.to_csv(stage2_csv, index=False)
    stage5_asset.to_csv(stage5_csv, index=False)
    stage6_windows.to_csv(stage6_csv, index=False)
    stage7_regimes["trend_regime"].to_csv(stage7_trend_csv, index=False)
    stage7_regimes["vol_regime"].to_csv(stage7_vol_csv, index=False)
    stage7_regimes["compression_regime"].to_csv(stage7_comp_csv, index=False)
    stage7_regimes["regime_combo"].to_csv(stage7_combo_csv, index=False)
    stage9_filters.to_csv(stage9_csv, index=False)
    stage9_filters_balanced.to_csv(stage9_bal_csv, index=False)
    stage10_importance.to_csv(stage10_csv, index=False)
    enriched.to_csv(enriched_csv, index=False)

    payload = {
        "generated_at": _now_utc().isoformat(),
        "campaign_id": cfg.campaign_id,
        "strategy_key": cfg.strategy_key,
        "execution_ids": execution_ids,
        "sample_size": int(len(enriched)),
        "stage3_breakout_quality": stage3_breakout,
        "stage4_mfe_mae": stage4_mfe_mae,
        "stage8_donchian": stage8_donchian,
        "final_answers": final_answers,
        "outputs": {
            "stage1_trade_table": str(stage1_csv),
            "stage2_win_loss_stats": str(stage2_csv),
            "stage5_asset_metrics": str(stage5_csv),
            "stage6_temporal_windows": str(stage6_csv),
            "stage7_trend_regime": str(stage7_trend_csv),
            "stage7_vol_regime": str(stage7_vol_csv),
            "stage7_compression_regime": str(stage7_comp_csv),
            "stage7_regime_combo": str(stage7_combo_csv),
            "stage9_filters": str(stage9_csv),
            "stage9_filters_balanced": str(stage9_bal_csv),
            "stage10_importance": str(stage10_csv),
            "enriched_trades": str(enriched_csv),
            "report_json": str(report_json),
            "report_md": str(report_md),
        },
    }

    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(
        _markdown_report(
            cfg,
            enriched,
            stage2_stats,
            stage3_breakout,
            stage4_mfe_mae,
            stage5_asset,
            stage6_windows,
            stage7_regimes,
            stage8_donchian,
            stage9_filters,
            stage9_filters_balanced,
            stage10_importance,
            final_answers,
        ),
        encoding="utf-8",
    )

    return payload


if __name__ == "__main__":
    output = run(Config())
    print(json.dumps(output, ensure_ascii=False, indent=2))
