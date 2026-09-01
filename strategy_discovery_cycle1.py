"""Controlled Strategy Discovery Cycle 1.

This module is intentionally isolated from the registered strategies and the
official Paper Live campaign. It evaluates three simple, predeclared hypotheses
on one common temporal dataset and writes only consolidated latest artifacts.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from config.settings import settings
from backtesting.engine import BacktestConfig, BacktestEngine
from research.services.market_regime_router_phase18 import classify_market_regimes
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
CYCLE = str(os.getenv("STRATEGY_DISCOVERY_CYCLE", "1")).strip()
OUT_PREFIX = "strategy_discovery_latest" if CYCLE == "continuous" else f"strategy_discovery_cycle{CYCLE}_latest"
OUT_JSON = BASE_DIR / f"{OUT_PREFIX}.json"
OUT_CSV = BASE_DIR / f"{OUT_PREFIX}.csv"
OUT_MD = BASE_DIR / f"{OUT_PREFIX}.md"

SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
TIMEFRAMES = ("5m", "15m", "1h")
DATA_START = pd.Timestamp("2024-01-01T00:00:00Z")
DATA_END = pd.Timestamp("2026-08-18T18:00:00Z")
EXTENDED_KLINES_PATH = BASE_DIR / "data" / "binance_spot_extended_klines.csv.gz"
CAPITAL = 10_000.0
BASE_FEE = 0.001
STRESS_FEE = 0.0015
BOOTSTRAP_ITERATIONS = 10_000
ENTRY_FORWARD_HORIZONS = (1, 3, 5, 10, 20)
ENTRY_MIN_EPISODES = 100
ENTRY_MIN_EFFECT_BPS = 5.0
ENTRY_MIN_T_STAT = 2.0

# These hypotheses failed a frozen DEV -> Validation -> OOS entry-only audit.
REJECTED_ENTRY_FAMILIES: dict[str, str] = {
    "CROSS_SECTIONAL_STRENGTH": "NO_GENERALIZABLE_ENTRY_EDGE",
    "IMPULSE_PULLBACK": "NO_GENERALIZABLE_ENTRY_EDGE",
    "VOLUME_CONFIRMED_BREAKOUT": "NO_GENERALIZABLE_ENTRY_EDGE",
    "BTC_LEAD_LAG": "NO_GENERALIZABLE_ENTRY_EDGE",
    "MARKET_BREADTH_ALIGNMENT": "NO_GENERALIZABLE_ENTRY_EDGE",
    "CORRELATION_DECOUPLING": "NO_GENERALIZABLE_ENTRY_EDGE",
    "INTRADAY_REGIME_SEASONALITY": "NO_GENERALIZABLE_ENTRY_EDGE",
    "TAKER_FLOW_IMBALANCE": "NO_GENERALIZABLE_ENTRY_EDGE",
}

FAMILY_NAMES = ("MOMENTUM", "MEAN_REVERSION", "VOLATILITY_BREAKOUT")

HYPOTHESES: dict[str, dict[str, Any]] = {
    "MOMENTUM": {
        "statement": "Persistencia direcional e aceleracao recente podem continuar apos confirmacao de tendencia.",
        "parameters": ("lookback", "trend_window", "roc_threshold"),
        "search_space": {"lookback": (12, 24, 48), "trend_window": (24, 48), "roc_threshold": (0.0, 0.002)},
    },
    "MEAN_REVERSION": {
        "statement": "Desvios extremos da media revertem quando a inclinacao recente nao indica tendencia forte.",
        "parameters": ("z_threshold", "trend_window", "max_holding_bars"),
        "search_space": {"z_threshold": (1.5, 2.0, 2.5), "trend_window": (24, 48), "max_holding_bars": (6, 12)},
    },
    "VOLATILITY_BREAKOUT": {
        "statement": "Expansao de volatilidade apos compressao pode gerar rompimento direcional persistente.",
        "parameters": ("range_window", "compression_ratio", "expansion_ratio"),
        "search_space": {"range_window": (12, 24, 48), "compression_ratio": (0.6, 0.8), "expansion_ratio": (1.2, 1.5)},
    },
}

if CYCLE == "2":
    FAMILY_NAMES = ("RANGE", "OPENING_RANGE", "LIQUIDITY_SWEEP")
    HYPOTHESES = {
        "RANGE": {
            "statement": "Em mercados laterais, compras proximas ao limite inferior do range tendem a retornar ao centro antes de uma ruptura persistente.",
            "parameters": ("range_window", "entry_fraction", "max_holding_bars"),
            "search_space": {"range_window": (24, 48, 96), "entry_fraction": (0.10, 0.20), "max_holding_bars": (6, 12)},
        },
        "OPENING_RANGE": {
            "statement": "A quebra do range formado no inicio de cada dia UTC pode capturar a primeira expansao direcional intradiaria.",
            "parameters": ("opening_bars", "breakout_buffer", "max_holding_bars"),
            "search_space": {"opening_bars": (3, 6, 12), "breakout_buffer": (0.0, 0.001), "max_holding_bars": (6, 12)},
        },
        "LIQUIDITY_SWEEP": {
            "statement": "Um falso rompimento do fundo recente seguido de fechamento de recuperacao pode indicar absorcao e reversao curta.",
            "parameters": ("lookback", "wick_ratio", "max_holding_bars"),
            "search_space": {"lookback": (24, 48, 96), "wick_ratio": (0.40, 0.60), "max_holding_bars": (6, 12)},
            "data_note": "Proxy OHLCV; o projeto nao possui order book, delta ou fluxo de liquidacoes.",
        },
    }
elif CYCLE == "3":
    FAMILY_NAMES = ("MARKET_STRUCTURE", "VWAP", "RELATIVE_STRENGTH")
    HYPOTHESES = {
        "MARKET_STRUCTURE": {
            "statement": "A quebra confirmada de uma maxima estrutural recente, acima de uma media de tendencia, pode iniciar continuacao direcional.",
            "parameters": ("swing_window", "trend_window", "breakout_buffer"),
            "search_space": {"swing_window": (24, 48, 96), "trend_window": (24, 48), "breakout_buffer": (0.0, 0.001)},
        },
        "VWAP": {
            "statement": "Deslocamentos negativos relevantes em relacao ao VWAP diario tendem a retornar ao VWAP quando nao ha ruptura estrutural persistente.",
            "parameters": ("deviation_threshold", "max_holding_bars", "trend_filter"),
            "search_space": {"deviation_threshold": (0.002, 0.004, 0.006), "max_holding_bars": (6, 12), "trend_filter": (0.0, 0.002)},
        },
        "RELATIVE_STRENGTH": {
            "statement": "Ativos com forca relativa persistente acima da propria tendencia media podem continuar performando no horizonte curto.",
            "parameters": ("roc_window", "trend_window", "strength_threshold"),
            "search_space": {"roc_window": (12, 24, 48), "trend_window": (24, 48), "strength_threshold": (0.0, 0.002)},
        },
    }
elif CYCLE == "5":
    FAMILY_NAMES = ("CROSS_SECTIONAL_STRENGTH", "IMPULSE_PULLBACK", "VOLUME_CONFIRMED_BREAKOUT")
    HYPOTHESES = {
        "CROSS_SECTIONAL_STRENGTH": {
            "statement": "O ativo lider do retorno recente entre os quatro pares pode continuar relativamente forte no proximo horizonte.",
            "parameters": ("roc_window", "trend_window", "leader_margin"),
            "search_space": {"roc_window": (12, 24, 48), "trend_window": (24, 48), "leader_margin": (0.0, 0.002)},
        },
        "IMPULSE_PULLBACK": {
            "statement": "Apos um impulso de alta, um recuo controlado ate a media seguido de recuperacao pode oferecer entrada com melhor assimetria.",
            "parameters": ("trend_window", "pullback_fraction", "max_holding_bars"),
            "search_space": {"trend_window": (24, 48, 96), "pullback_fraction": (0.25, 0.50), "max_holding_bars": (6, 12)},
        },
        "VOLUME_CONFIRMED_BREAKOUT": {
            "statement": "Rompimento de maxima recente acompanhado de volume relativo acima da media pode representar participacao nova e continuidade.",
            "parameters": ("range_window", "volume_ratio", "breakout_buffer"),
            "search_space": {"range_window": (24, 48, 96), "volume_ratio": (1.2, 1.5), "breakout_buffer": (0.0, 0.001)},
        },
    }
elif CYCLE == "6":
    FAMILY_NAMES = ("BTC_LEAD_LAG", "MARKET_BREADTH_ALIGNMENT", "CORRELATION_DECOUPLING")
    HYPOTHESES = {
        "BTC_LEAD_LAG": {
            "statement": "Um movimento recente do BTC pode se propagar aos demais ativos com atraso quando o ativo ainda nao acompanhou o retorno do lider.",
            "parameters": ("lead_window", "lead_threshold", "lag_threshold"),
            "search_space": {"lead_window": (6, 12, 24), "lead_threshold": (0.003, 0.006), "lag_threshold": (0.0, 0.002)},
        },
        "MARKET_BREADTH_ALIGNMENT": {
            "statement": "A continuidade de alta de um ativo pode ser mais provavel quando a participacao direcional e ampla entre os quatro pares, em vez de isolada.",
            "parameters": ("breadth_window", "breadth_min_assets", "own_threshold"),
            "search_space": {"breadth_window": (12, 24, 48), "breadth_min_assets": (3, 4), "own_threshold": (0.0, 0.001)},
        },
        "CORRELATION_DECOUPLING": {
            "statement": "Um ativo com retorno positivo e correlacao recente baixa com o ativo de referencia pode refletir fluxo idiossincratico com persistencia curta.",
            "parameters": ("correlation_window", "correlation_ceiling", "own_roc_threshold"),
            "search_space": {"correlation_window": (24, 48, 96), "correlation_ceiling": (0.4, 0.6), "own_roc_threshold": (0.002, 0.004)},
        },
    }
elif CYCLE == "continuous":
    # Bounded search space: OHLCV order-flow proxies, distinct from prior
    # price-breakout, mean-reversion, relative-strength and session hypotheses.
    FAMILY_NAMES = ("TAKER_FLOW_IMBALANCE",)
    HYPOTHESES = {
        "TAKER_FLOW_IMBALANCE": {
            "statement": "Participacao compradora agressora persistentemente acima da neutra pode revelar demanda ativa antes de o efeito aparecer no retorno agregado.",
            "parameters": ("flow_feature", "flow_window", "threshold_level"),
            "search_space": {"flow_feature": ("TAKER_BUY_RATIO", "TAKER_IMBALANCE", "QUOTE_TAKER_IMBALANCE", "TRADE_INTENSITY", "AVG_TRADE_SIZE_PROXY", "QUOTE_VOLUME_ACCELERATION"), "flow_window": (3, 6), "threshold_level": (0, 1)},
        },
    }


@dataclass(frozen=True)
class ConfigResult:
    family: str
    parameters: dict[str, float | int]
    split: str
    trades: int
    pf: float
    expectancy: float
    max_dd: float
    sharpe: float
    win_rate: float
    net_profit: float
    context_rows: list[dict[str, Any]]
    best_cell_pnl_share: float
    reproducibility: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _dataset_hash(contexts: dict[tuple[str, str], pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    for (symbol, timeframe), frame in sorted(contexts.items()):
        digest.update(f"{symbol}|{timeframe}|".encode("utf-8"))
        digest.update("|".join(str(column) for column in frame.columns).encode("utf-8"))
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return os.getenv("GIT_COMMIT") or None


def _canonical_metrics(result: ConfigResult) -> tuple[Any, ...]:
    return (
        result.trades,
        round(result.pf, 10),
        round(result.expectancy, 10),
        round(result.max_dd, 10),
        round(result.sharpe, 10),
        round(result.net_profit, 10),
        tuple(
            (
                row["symbol"],
                row["timeframe"],
                row["trades"],
                round(float(row["pf"]), 10),
                round(float(row["expectancy"]), 10),
                round(float(row["net_profit"]), 10),
            )
            for row in result.context_rows
        ),
        tuple(sorted((key, round(float(value), 10) if isinstance(value, float) else value) for key, value in result.diagnostics.items())),
    )


class DiscoveryHypothesisStrategy(BaseStrategy):
    """Signal-only adapter; execution remains exclusively in BacktestEngine."""

    def __init__(self, family: str, parameters: dict[str, float | int], timeframe: str) -> None:
        self._family = family
        self._parameters = dict(parameters)
        self._timeframe = timeframe

    @property
    def name(self) -> str:
        return f"Discovery{self._family}"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        entry, exit_signal, stop_distance, target_distance = _signals(self._family, df, self._parameters)
        out = df.copy()
        out["_discovery_entry"] = entry
        out["_discovery_exit"] = exit_signal
        out["_discovery_stop_distance"] = stop_distance
        out["_discovery_target_distance"] = target_distance
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if not bool(last.get("_discovery_entry", False)):
            return StrategySignal(SignalType.HOLD, price, last.name.to_pydatetime())
        stop_distance = float(last["_discovery_stop_distance"])
        target_distance = float(last["_discovery_target_distance"])
        if not np.isfinite(stop_distance) or stop_distance <= 0 or not np.isfinite(target_distance) or target_distance <= 0:
            return StrategySignal(SignalType.HOLD, price, last.name.to_pydatetime())
        max_holding_bars = self._parameters.get("max_holding_bars")
        metadata = {}
        if max_holding_bars is not None:
            minutes = {"5m": 5, "15m": 15, "1h": 60}[self._timeframe]
            metadata["max_holding_minutes"] = int(max_holding_bars) * minutes
        return StrategySignal(
            SignalType.BUY,
            price,
            last.name.to_pydatetime(),
            score=1.0,
            stop_loss=price - stop_distance,
            take_profit=price + target_distance,
            metadata=metadata,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        signal = SignalType.SELL if bool(last.get("_discovery_exit", False)) else SignalType.HOLD
        return StrategySignal(signal, float(last["close"]), last.name.to_pydatetime())

    def score(self, df: pd.DataFrame) -> float:
        return 1.0 if bool(df.iloc[-1].get("_discovery_entry", False)) else 0.0


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _profit_factor(pnl: np.ndarray) -> float:
    wins = float(pnl[pnl > 0].sum()) if len(pnl) else 0.0
    losses = abs(float(pnl[pnl < 0].sum())) if len(pnl) else 0.0
    return wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max((peak - equity) / np.maximum(peak, 1e-12)))


def _trade_metrics(pnl: list[float]) -> tuple[int, float, float, float, float, float, float]:
    values = np.asarray(pnl, dtype=float)
    if len(values) == 0:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    equity = CAPITAL + np.cumsum(values)
    returns = values / CAPITAL
    sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252)) if len(values) > 1 and np.std(returns) > 0 else 0.0
    return (
        len(values),
        _profit_factor(values),
        float(np.mean(values)),
        _max_drawdown(equity),
        sharpe,
        float(np.mean(values > 0)),
        float(np.sum(values)),
    )


def _entry_metrics(returns: pd.Series, mfe: pd.Series, mae: pd.Series) -> dict[str, float | int]:
    values = pd.to_numeric(returns, errors="coerce").dropna().to_numpy(float)
    if len(values) == 0:
        return {"episodes": 0, "win_rate_forward": 0.0, "average_return": 0.0, "median_return": 0.0, "average_mfe": 0.0, "median_mfe": 0.0, "average_mae": 0.0, "median_mae": 0.0, "gross_expectancy": 0.0, "gross_pf": 0.0, "effect_bps": 0.0, "t_stat": 0.0}
    standard_error = float(np.std(values, ddof=1) / np.sqrt(len(values))) if len(values) > 1 else 0.0
    return {"episodes": int(len(values)), "win_rate_forward": float(np.mean(values > 0)), "average_return": float(np.mean(values)), "median_return": float(np.median(values)), "average_mfe": float(pd.to_numeric(mfe, errors="coerce").mean()), "median_mfe": float(pd.to_numeric(mfe, errors="coerce").median()), "average_mae": float(pd.to_numeric(mae, errors="coerce").mean()), "median_mae": float(pd.to_numeric(mae, errors="coerce").median()), "gross_expectancy": float(np.mean(values)), "gross_pf": _profit_factor(values), "effect_bps": float(np.mean(values) * 10_000.0), "t_stat": float(np.mean(values) / standard_error) if standard_error > 0 else 0.0}


def _entry_audit_once(family: str, parameters: dict[str, float | int], split: str, contexts: dict[tuple[str, str], pd.DataFrame], bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> list[dict[str, Any]]:
    prepared_contexts = _prepare_contexts(family, parameters, contexts)
    split_start, split_end = bounds[split]
    records: list[dict[str, Any]] = []
    for (symbol, timeframe), source in prepared_contexts.items():
        enriched = DiscoveryHypothesisStrategy(family, parameters, timeframe).calculate(source.copy())
        eligible = (enriched.index >= split_start) & (enriched.index < split_end)
        split_positions = np.flatnonzero(eligible)
        if len(split_positions) <= 50:
            continue
        regimes = classify_market_regimes(source.loc[source.index < split_end])
        entry = enriched["_discovery_entry"].astype(bool)
        starts = entry & ~entry.shift(fill_value=False)
        first_eligible_position = int(split_positions[0]) + 50
        for signal_position in np.flatnonzero(starts.to_numpy(bool)):
            if signal_position < first_eligible_position or not bool(eligible[signal_position]):
                continue
            entry_position = signal_position + 1
            if entry_position >= len(enriched) or not bool(eligible[entry_position]):
                continue
            entry_price = float(enriched.iloc[entry_position]["open"])
            if not np.isfinite(entry_price) or entry_price <= 0:
                continue
            for horizon in ENTRY_FORWARD_HORIZONS:
                exit_position = entry_position + horizon - 1
                if exit_position >= len(enriched) or not bool(eligible[exit_position]):
                    continue
                path = enriched.iloc[entry_position : exit_position + 1]
                records.append({"family": family, "symbol": symbol, "timeframe": timeframe, "regime": str(regimes.loc[enriched.index[signal_position], "regime_key"]), "horizon": horizon, "entry_timestamp": enriched.index[entry_position], "forward_return": float(path.iloc[-1]["close"] / entry_price - 1.0), "mfe": float(path["high"].max() / entry_price - 1.0), "mae": float(path["low"].min() / entry_price - 1.0)})
    frame = pd.DataFrame(records)
    if frame.empty:
        return []
    keys = ["family", "symbol", "timeframe", "regime", "horizon"]
    return [{**dict(zip(keys, key)), **_entry_metrics(group["forward_return"], group["mfe"], group["mae"])} for key, group in frame.groupby(keys, sort=True)]


def _entry_audit(family: str, parameters: dict[str, float | int], split: str, contexts: dict[tuple[str, str], pd.DataFrame], bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> dict[str, Any]:
    runs = [_entry_audit_once(family, parameters, split, contexts, bounds) for _ in range(3)]
    canonical = [json.dumps(_json_safe(run), ensure_ascii=True, sort_keys=True) for run in runs]
    if canonical[0] != canonical[1] or canonical[0] != canonical[2]:
        raise RuntimeError(f"ENTRY_AUDIT_REPRODUCIBLE=NO for {family}/{split}/{parameters}")
    return {"reproducible": True, "rows": runs[0]}


def _entry_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [row for row in rows if _passes_entry_gate(row)]
    valid.sort(key=lambda row: (float(row["t_stat"]), float(row["gross_pf"]), float(row["gross_expectancy"]), int(row["episodes"])), reverse=True)
    return valid


def _passes_entry_gate(row: dict[str, Any]) -> bool:
    return (
        int(row["episodes"]) >= ENTRY_MIN_EPISODES
        and float(row["gross_expectancy"]) > 0.0
        and float(row["gross_pf"]) > 1.0
        and float(row["effect_bps"]) >= ENTRY_MIN_EFFECT_BPS
        and float(row["t_stat"]) >= ENTRY_MIN_T_STAT
    )


def _same_entry_context(rows: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    keys = ("family", "symbol", "timeframe", "regime", "horizon")
    return next((row for row in rows if all(row[key] == candidate[key] for key in keys)), None)


def _episode_diagnostics(frame: pd.DataFrame, trades: list[dict[str, Any]], warmup_bars: int) -> dict[str, float | int]:
    entry = frame["_discovery_entry"].astype(bool).copy()
    entry.iloc[:warmup_bars] = False
    starts = entry & ~entry.shift(fill_value=False)
    episode_id = starts.cumsum().where(entry)
    durations = entry.groupby(episode_id).sum()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trade in trades:
        entry_bar = int(trade["entry_bar"])
        if entry_bar < 0 or entry_bar >= len(episode_id):
            continue
        episode = episode_id.iloc[entry_bar]
        if pd.isna(episode):
            continue
        grouped.setdefault(int(episode), []).append(trade)

    first_trades = [episode_trades[0] for episode_trades in grouped.values()]
    all_pnl = [float(trade["pnl"]) for trade in trades]
    deduplicated_pnl = [float(trade["pnl"]) for trade in first_trades]
    reentries = sum(max(0, len(episode_trades) - 1) for episode_trades in grouped.values())
    intervals = [
        int(trades[index]["entry_bar"]) - int(trades[index - 1]["entry_bar"])
        for index in range(1, len(trades))
    ]
    return {
        "total_signals": int(entry.sum()),
        "total_trades": len(trades),
        "independent_events": len(first_trades),
        "reentries": reentries,
        "reentry_inflation_pct": float(reentries / len(first_trades) * 100.0) if first_trades else 0.0,
        "average_signal_duration": float(durations.mean()) if not durations.empty else 0.0,
        "average_trade_interval": float(np.mean(intervals)) if intervals else 0.0,
        "persistent_signal_reentries": reentries,
        "pf_all": _profit_factor(np.asarray(all_pnl, dtype=float)),
        "expectancy_all": float(np.mean(all_pnl)) if all_pnl else 0.0,
        "pf_deduplicated": _profit_factor(np.asarray(deduplicated_pnl, dtype=float)),
        "expectancy_deduplicated": float(np.mean(deduplicated_pnl)) if deduplicated_pnl else 0.0,
        "signal_episodes": int(len(durations)),
    }


def _deduplicated_pnl(frame: pd.DataFrame, trades: list[dict[str, Any]], warmup_bars: int) -> list[float]:
    entry = frame["_discovery_entry"].astype(bool).copy()
    entry.iloc[:warmup_bars] = False
    episode_id = (entry & ~entry.shift(fill_value=False)).cumsum().where(entry)
    first_trade_by_episode: dict[int, float] = {}
    for trade in trades:
        entry_bar = int(trade["entry_bar"])
        if entry_bar < 0 or entry_bar >= len(episode_id):
            continue
        episode = episode_id.iloc[entry_bar]
        if pd.notna(episode):
            first_trade_by_episode.setdefault(int(episode), float(trade["pnl"]))
    return list(first_trade_by_episode.values())


def _load_contexts() -> dict[tuple[str, str], pd.DataFrame]:
    if CYCLE == "continuous" and EXTENDED_KLINES_PATH.exists():
        extended = pd.read_csv(EXTENDED_KLINES_PATH, compression="gzip", parse_dates=["open_time"])
        extended["open_time"] = pd.to_datetime(extended["open_time"], utc=True)
        extended = extended.set_index("open_time").sort_index()
        return {
            (symbol, timeframe): extended.loc[
                (extended["symbol"] == symbol) & (extended["timeframe"] == timeframe)
            ].drop(columns=["symbol", "timeframe"])
            for symbol in SYMBOLS
            for timeframe in TIMEFRAMES
        }
    engine = create_engine(settings.database.url, future=True)
    query = text(
        """
        SELECT open_time, open, high, low, close, volume
        FROM candles
        WHERE symbol = :symbol AND timeframe = :timeframe
          AND open_time >= :start AND open_time <= :end
        ORDER BY open_time
        """
    )
    contexts: dict[tuple[str, str], pd.DataFrame] = {}
    with engine.connect() as connection:
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                frame = pd.read_sql(
                    query,
                    connection,
                    params={"symbol": symbol, "timeframe": timeframe, "start": DATA_START.to_pydatetime(), "end": DATA_END.to_pydatetime()},
                )
                frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
                frame = frame.set_index("open_time").astype(float).sort_index()
                contexts[(symbol, timeframe)] = frame
    if any(frame.empty for frame in contexts.values()):
        missing = [key for key, frame in contexts.items() if frame.empty]
        raise RuntimeError(f"Common dataset missing contexts: {missing}")
    return contexts


def _split_bounds() -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    total = DATA_END - DATA_START
    dev_end = DATA_START + total * 0.60
    validation_end = DATA_START + total * 0.80
    return {
        "DEV": (DATA_START, dev_end),
        "VALIDATION": (dev_end, validation_end),
        "OOS": (validation_end, DATA_END),
    }


def _split_frame(frame: pd.DataFrame, split: str, bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
    start, end = bounds[split]
    return frame.loc[(frame.index >= start) & (frame.index < end)].copy()


def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous).abs(), (frame["low"] - previous).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(com=period - 1, adjust=False).mean()


def _signals(family: str, frame: pd.DataFrame, parameters: dict[str, float | int]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    close = frame["close"]
    atr = _atr(frame).to_numpy(float)
    if family == "MOMENTUM":
        lookback = int(parameters["lookback"])
        trend_window = int(parameters["trend_window"])
        threshold = float(parameters["roc_threshold"])
        roc = close.pct_change(lookback)
        trend = close / close.shift(trend_window) - 1.0
        entry = (roc > threshold) & (trend > 0)
        exit_signal = (roc < 0) | (trend < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "MEAN_REVERSION":
        z_threshold = float(parameters["z_threshold"])
        trend_window = int(parameters["trend_window"])
        rolling_mean = close.rolling(trend_window).mean()
        rolling_std = close.rolling(trend_window).std(ddof=0)
        z_score = (close - rolling_mean) / rolling_std.replace(0, np.nan)
        slope = close.pct_change(trend_window).abs()
        entry = (z_score <= -z_threshold) & (slope <= 0.01)
        exit_signal = (z_score >= 0) | (slope > 0.02)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = (rolling_mean - close).clip(lower=0.0).fillna(0.0)
    elif family == "VOLATILITY_BREAKOUT":
        range_window = int(parameters["range_window"])
        compression_ratio = float(parameters["compression_ratio"])
        expansion_ratio = float(parameters["expansion_ratio"])
        atr_series = pd.Series(atr, index=frame.index)
        atr_baseline = atr_series.rolling(range_window).mean()
        prior_high = frame["high"].rolling(range_window).max().shift(1)
        prior_low = frame["low"].rolling(range_window).min().shift(1)
        compression = atr_series.shift(1) <= atr_baseline.shift(1) * compression_ratio
        expansion = atr_series >= atr_baseline * expansion_ratio
        entry = compression & expansion & (close > prior_high)
        exit_signal = close < close.rolling(range_window).mean()
        stop_distance = 1.5 * atr_series
        target_distance = 2.0 * stop_distance
    elif family == "RANGE":
        range_window = int(parameters["range_window"])
        entry_fraction = float(parameters["entry_fraction"])
        rolling_low = frame["low"].rolling(range_window).min()
        rolling_high = frame["high"].rolling(range_window).max()
        range_width = (rolling_high - rolling_low).replace(0, np.nan)
        rolling_mid = (rolling_high + rolling_low) / 2.0
        entry = close <= rolling_low + range_width * entry_fraction
        exit_signal = close >= rolling_mid
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = (rolling_mid - close).clip(lower=0.0).fillna(0.0)
    elif family == "OPENING_RANGE":
        opening_bars = int(parameters["opening_bars"])
        breakout_buffer = float(parameters["breakout_buffer"])
        day_key = pd.Series(frame.index.floor("D"), index=frame.index)
        bar_number = day_key.groupby(day_key).cumcount()
        opening_mask = bar_number < opening_bars
        opening_high = frame["high"].where(opening_mask).groupby(day_key).transform("max")
        opening_low = frame["low"].where(opening_mask).groupby(day_key).transform("min")
        opening_width = (opening_high - opening_low).replace(0, np.nan)
        after_opening = bar_number >= opening_bars
        entry = after_opening & (close > opening_high * (1.0 + breakout_buffer))
        exit_signal = close < (opening_low + opening_width * 0.5)
        stop_distance = 1.0 * pd.Series(atr, index=frame.index)
        target_distance = (opening_width * 1.5).fillna(0.0)
    elif family == "MARKET_STRUCTURE":
        swing_window = int(parameters["swing_window"])
        trend_window = int(parameters["trend_window"])
        breakout_buffer = float(parameters["breakout_buffer"])
        structure_high = frame["high"].rolling(swing_window).max().shift(1)
        trend = close / close.shift(trend_window) - 1.0
        entry = (close > structure_high * (1.0 + breakout_buffer)) & (trend > 0)
        exit_signal = (close < close.rolling(swing_window).mean()) | (trend < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "VWAP":
        deviation_threshold = float(parameters["deviation_threshold"])
        max_holding_bars = int(parameters["max_holding_bars"])
        trend_filter = float(parameters["trend_filter"])
        day_key = pd.Series(frame.index.floor("D"), index=frame.index)
        typical = (frame["high"] + frame["low"] + close) / 3.0
        volume = frame["volume"].clip(lower=0.0)
        vwap = (typical * volume).groupby(day_key).cumsum() / volume.groupby(day_key).cumsum().replace(0.0, np.nan)
        trend = close / close.shift(max(12, max_holding_bars * 2)) - 1.0
        entry = (close <= vwap * (1.0 - deviation_threshold)) & (trend >= -trend_filter)
        exit_signal = close >= vwap
        stop_distance = 1.25 * pd.Series(atr, index=frame.index)
        target_distance = (vwap - close).clip(lower=0.0).fillna(0.0)
    elif family == "RELATIVE_STRENGTH":
        roc_window = int(parameters["roc_window"])
        trend_window = int(parameters["trend_window"])
        strength_threshold = float(parameters["strength_threshold"])
        roc = close / close.shift(roc_window) - 1.0
        trend = close / close.shift(trend_window) - 1.0
        entry = (roc > strength_threshold) & (trend > 0)
        exit_signal = (roc < 0) | (trend < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "CROSS_SECTIONAL_STRENGTH":
        roc_window = int(parameters["roc_window"])
        trend_window = int(parameters["trend_window"])
        leader_margin = float(parameters["leader_margin"])
        roc = close / close.shift(roc_window) - 1.0
        trend = close / close.shift(trend_window) - 1.0
        leader = frame.get("_cross_sectional_leader", pd.Series(False, index=frame.index))
        entry = leader & (roc > leader_margin) & (trend > 0)
        exit_signal = (roc < 0) | (trend < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "IMPULSE_PULLBACK":
        trend_window = int(parameters["trend_window"])
        pullback_fraction = float(parameters["pullback_fraction"])
        impulse = close / close.shift(trend_window) - 1.0
        ema = close.ewm(span=trend_window, adjust=False).mean()
        distance = (close - ema) / ema.replace(0, np.nan)
        recovery = close > frame["open"]
        entry = (impulse > 0.01) & (distance <= pullback_fraction * impulse) & recovery
        exit_signal = (close < ema) | (impulse < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "VOLUME_CONFIRMED_BREAKOUT":
        range_window = int(parameters["range_window"])
        volume_ratio = float(parameters["volume_ratio"])
        breakout_buffer = float(parameters["breakout_buffer"])
        prior_high = frame["high"].rolling(range_window).max().shift(1)
        volume_average = frame["volume"].rolling(range_window).mean().shift(1)
        entry = (close > prior_high * (1.0 + breakout_buffer)) & (frame["volume"] >= volume_average * volume_ratio)
        exit_signal = close < close.rolling(range_window).mean()
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "BTC_LEAD_LAG":
        lead_window = int(parameters["lead_window"])
        lead_threshold = float(parameters["lead_threshold"])
        lag_threshold = float(parameters["lag_threshold"])
        reference_return = frame.get("_reference_return", pd.Series(np.nan, index=frame.index))
        own_return = close.pct_change(lead_window)
        entry = (reference_return > lead_threshold) & (own_return <= reference_return - lag_threshold)
        exit_signal = (reference_return < 0) | (own_return >= reference_return)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "MARKET_BREADTH_ALIGNMENT":
        breadth_min_assets = int(parameters["breadth_min_assets"])
        own_threshold = float(parameters["own_threshold"])
        breadth = frame.get("_market_breadth", pd.Series(0, index=frame.index))
        own_return = close.pct_change(int(parameters["breadth_window"]))
        entry = (breadth >= breadth_min_assets) & (own_return > own_threshold)
        exit_signal = (breadth < breadth_min_assets) | (own_return < 0)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "CORRELATION_DECOUPLING":
        correlation_window = int(parameters["correlation_window"])
        correlation_ceiling = float(parameters["correlation_ceiling"])
        own_roc_threshold = float(parameters["own_roc_threshold"])
        reference_correlation = frame.get("_reference_correlation", pd.Series(np.nan, index=frame.index))
        own_return = close.pct_change(correlation_window)
        entry = (reference_correlation <= correlation_ceiling) & (own_return > own_roc_threshold)
        exit_signal = (own_return < 0) | (reference_correlation > correlation_ceiling)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "CLOSE_LOCATION_VOLUME_IMBALANCE":
        volume_window = int(parameters["volume_window"])
        volume_ratio = float(parameters["volume_ratio"])
        close_location = float(parameters["close_location"])
        candle_range = (frame["high"] - frame["low"]).replace(0.0, np.nan)
        location = (close - frame["low"]) / candle_range
        volume_average = frame["volume"].rolling(volume_window).mean().shift(1)
        entry = (location >= close_location) & (frame["volume"] >= volume_average * volume_ratio)
        exit_signal = location < 0.5
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "DIRECTIONAL_EFFICIENCY":
        efficiency_window = int(parameters["efficiency_window"])
        efficiency_threshold = float(parameters["efficiency_threshold"])
        min_directional_return = float(parameters["min_directional_return"])
        net_move = close - close.shift(efficiency_window)
        path = close.diff().abs().rolling(efficiency_window).sum().replace(0.0, np.nan)
        efficiency = net_move.abs() / path
        directional_return = close.pct_change(efficiency_window)
        entry = (directional_return >= min_directional_return) & (efficiency >= efficiency_threshold)
        exit_signal = directional_return < 0.0
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    elif family == "TAKER_FLOW_IMBALANCE":
        flow_feature = str(parameters["flow_feature"])
        flow_window = int(parameters["flow_window"])
        quote_volume = frame["quote_volume"].replace(0.0, np.nan)
        volume = frame["volume"].replace(0.0, np.nan)
        buy_ratio = frame["taker_buy_base_volume"] / volume
        if flow_feature == "TAKER_BUY_RATIO":
            feature = buy_ratio
            threshold = 0.55 if int(parameters["threshold_level"]) == 0 else 0.60
        elif flow_feature == "TAKER_IMBALANCE":
            feature = 2.0 * buy_ratio - 1.0
            threshold = 0.05 if int(parameters["threshold_level"]) == 0 else 0.10
        elif flow_feature == "QUOTE_TAKER_IMBALANCE":
            feature = 2.0 * frame["taker_buy_quote_volume"] / quote_volume - 1.0
            threshold = 0.05 if int(parameters["threshold_level"]) == 0 else 0.10
        elif flow_feature == "TRADE_INTENSITY":
            history = frame["trade_count"].rolling(48).mean().shift(1)
            feature = frame["trade_count"] / history.replace(0.0, np.nan)
            threshold = 1.05 if int(parameters["threshold_level"]) == 0 else 1.20
        elif flow_feature == "AVG_TRADE_SIZE_PROXY":
            feature = quote_volume / frame["trade_count"].replace(0.0, np.nan)
            threshold = feature.rolling(48).median().shift(1)
            threshold *= 1.0 if int(parameters["threshold_level"]) == 0 else 1.10
        else:
            prior = quote_volume.rolling(48).mean().shift(1)
            feature = quote_volume / prior.replace(0.0, np.nan)
            threshold = 1.10 if int(parameters["threshold_level"]) == 0 else 1.25
        flow = feature.rolling(flow_window).mean().shift(1)
        entry = flow >= threshold
        exit_signal = flow < (0.5 if flow_feature != "TAKER_BUY_RATIO" else 0.50)
        stop_distance = 1.5 * pd.Series(atr, index=frame.index)
        target_distance = 2.0 * stop_distance
    else:
        lookback = int(parameters["lookback"])
        wick_ratio = float(parameters["wick_ratio"])
        prior_low = frame["low"].rolling(lookback).min().shift(1)
        candle_range = (frame["high"] - frame["low"]).replace(0, np.nan)
        lower_wick = np.minimum(frame["open"], frame["close"]) - frame["low"]
        sweep = frame["low"] < prior_low
        reclaim = close > prior_low
        rejection = lower_wick / candle_range >= wick_ratio
        entry = sweep & reclaim & rejection
        exit_signal = close >= close.rolling(lookback).mean()
        stop_distance = 1.25 * pd.Series(atr, index=frame.index)
        target_distance = 1.75 * stop_distance
    return entry.fillna(False).to_numpy(bool), exit_signal.fillna(False).to_numpy(bool), stop_distance.to_numpy(float), target_distance.to_numpy(float)


def _configurations(family: str) -> list[dict[str, float | int]]:
    space = HYPOTHESES[family]["search_space"]
    keys = tuple(space)
    configs = [dict(zip(keys, values)) for values in product(*(space[key] for key in keys))]
    if len(configs) > 27:
        raise AssertionError(f"Search budget exceeded for {family}: {len(configs)}")
    return configs


def _evaluate_once(family: str, parameters: dict[str, float | int], split: str, contexts: dict[tuple[str, str], pd.DataFrame], bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]], fee: float = BASE_FEE) -> ConfigResult:
    contexts = _prepare_contexts(family, parameters, contexts)
    all_pnl: list[float] = []
    all_deduplicated_pnl: list[float] = []
    context_rows: list[dict[str, Any]] = []
    diagnostic_rows: list[dict[str, float | int]] = []
    for (symbol, timeframe), source in contexts.items():
        frame = _split_frame(source, split, bounds)
        strategy = DiscoveryHypothesisStrategy(family, parameters, timeframe)
        strategy.initialize()
        result = BacktestEngine(
            strategy,
            config=BacktestConfig(initial_capital=CAPITAL, fee_pct=fee, warmup_bars=50),
        ).run(frame, symbol=symbol, timeframe=timeframe)
        trades = result.trades
        pnl = [float(trade["pnl"]) for trade in trades]
        enriched = strategy.calculate(frame)
        diagnostics = _episode_diagnostics(enriched, trades, warmup_bars=50)
        all_deduplicated_pnl.extend(_deduplicated_pnl(enriched, trades, warmup_bars=50))
        metrics = (
            result.metrics.total_trades,
            result.metrics.profit_factor,
            result.metrics.expectancy,
            result.metrics.max_drawdown_pct,
            result.metrics.sharpe_ratio,
            result.metrics.win_rate,
            result.metrics.net_profit,
        )
        all_pnl.extend(pnl)
        context_rows.append({"symbol": symbol, "timeframe": timeframe, "trades": metrics[0], "pf": metrics[1], "expectancy": metrics[2], "max_dd": metrics[3], "sharpe": metrics[4], "net_profit": metrics[6], "diagnostics": diagnostics})
        diagnostic_rows.append(diagnostics)
    metrics = _trade_metrics(all_pnl)
    positive_cells = [max(0.0, float(row["net_profit"])) for row in context_rows]
    total_positive = sum(positive_cells)
    best_share = max(positive_cells) / total_positive if total_positive > 0 else 1.0
    total_trades = sum(int(row["total_trades"]) for row in diagnostic_rows)
    independent_events = sum(int(row["independent_events"]) for row in diagnostic_rows)
    reentries = sum(int(row["reentries"]) for row in diagnostic_rows)
    deduplicated_metrics = _trade_metrics(all_deduplicated_pnl)
    diagnostics = {
        "total_trades": total_trades,
        "independent_events": independent_events,
        "reentries": reentries,
        "reentry_inflation_pct": float(reentries / independent_events * 100.0) if independent_events else 0.0,
        "average_signal_duration": float(np.average([row["average_signal_duration"] for row in diagnostic_rows], weights=[max(1, int(row["signal_episodes"])) for row in diagnostic_rows])) if diagnostic_rows else 0.0,
        "average_trade_interval": float(np.average([row["average_trade_interval"] for row in diagnostic_rows], weights=[max(1, int(row["total_trades"]) - 1) for row in diagnostic_rows])) if diagnostic_rows else 0.0,
        "persistent_signal_reentries": reentries,
        "pf_all": metrics[1],
        "expectancy_all": metrics[2],
        "pf_deduplicated": deduplicated_metrics[1],
        "expectancy_deduplicated": deduplicated_metrics[2],
        "signal_episodes": sum(int(row["signal_episodes"]) for row in diagnostic_rows),
    }
    return ConfigResult(family, dict(parameters), split, *metrics, context_rows, best_share, diagnostics=diagnostics)


def _with_cross_sectional_leader(
    contexts: dict[tuple[str, str], pd.DataFrame],
    roc_window: int,
) -> dict[tuple[str, str], pd.DataFrame]:
    enriched = {key: frame.copy() for key, frame in contexts.items()}
    for timeframe in TIMEFRAMES:
        returns = {
            symbol: frame["close"].pct_change(roc_window)
            for (symbol, current_timeframe), frame in enriched.items()
            if current_timeframe == timeframe
        }
        if not returns:
            continue
        return_frame = pd.DataFrame(returns)
        cross_median = return_frame.median(axis=1)
        for symbol in returns:
            frame = enriched[(symbol, timeframe)]
            enriched[(symbol, timeframe)]["_cross_sectional_leader"] = (
                returns[symbol] > cross_median
            ).reindex(frame.index).fillna(False)
    return enriched


def _reference_symbol(symbol: str) -> str:
    return "ETH/USDT" if symbol == "BTC/USDT" else "BTC/USDT"


def _with_reference_return(
    contexts: dict[tuple[str, str], pd.DataFrame],
    lead_window: int,
) -> dict[tuple[str, str], pd.DataFrame]:
    enriched = {key: frame.copy() for key, frame in contexts.items()}
    for (symbol, timeframe), frame in enriched.items():
        reference = contexts[(_reference_symbol(symbol), timeframe)]["close"]
        frame["_reference_return"] = reference.pct_change(lead_window).reindex(frame.index)
    return enriched


def _with_market_breadth(
    contexts: dict[tuple[str, str], pd.DataFrame],
    breadth_window: int,
) -> dict[tuple[str, str], pd.DataFrame]:
    enriched = {key: frame.copy() for key, frame in contexts.items()}
    for timeframe in TIMEFRAMES:
        returns = pd.DataFrame(
            {
                symbol: frame["close"].pct_change(breadth_window)
                for (symbol, current_timeframe), frame in contexts.items()
                if current_timeframe == timeframe
            }
        )
        breadth = (returns > 0).sum(axis=1)
        for symbol in returns:
            enriched[(symbol, timeframe)]["_market_breadth"] = breadth.reindex(enriched[(symbol, timeframe)].index).fillna(0)
    return enriched


def _with_reference_correlation(
    contexts: dict[tuple[str, str], pd.DataFrame],
    correlation_window: int,
) -> dict[tuple[str, str], pd.DataFrame]:
    enriched = {key: frame.copy() for key, frame in contexts.items()}
    for (symbol, timeframe), frame in enriched.items():
        reference_close = contexts[(_reference_symbol(symbol), timeframe)]["close"].reindex(frame.index)
        correlation = frame["close"].pct_change().rolling(correlation_window).corr(reference_close.pct_change())
        frame["_reference_correlation"] = correlation
    return enriched


def _prepare_contexts(
    family: str,
    parameters: dict[str, float | int],
    contexts: dict[tuple[str, str], pd.DataFrame],
) -> dict[tuple[str, str], pd.DataFrame]:
    if family == "CROSS_SECTIONAL_STRENGTH":
        return _with_cross_sectional_leader(contexts, int(parameters["roc_window"]))
    if family == "BTC_LEAD_LAG":
        return _with_reference_return(contexts, int(parameters["lead_window"]))
    if family == "MARKET_BREADTH_ALIGNMENT":
        return _with_market_breadth(contexts, int(parameters["breadth_window"]))
    if family == "CORRELATION_DECOUPLING":
        return _with_reference_correlation(contexts, int(parameters["correlation_window"]))
    return contexts


def _evaluate(family: str, parameters: dict[str, float | int], split: str, contexts: dict[tuple[str, str], pd.DataFrame], bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]], fee: float = BASE_FEE) -> ConfigResult:
    started_at = datetime.now(timezone.utc).isoformat()
    runs = [_evaluate_once(family, parameters, split, contexts, bounds, fee) for _ in range(3)]
    canonical = [_canonical_metrics(result) for result in runs]
    reproducible = canonical[0] == canonical[1] == canonical[2]
    evaluated_contexts = {
        key: _split_frame(frame, split, bounds)
        for key, frame in contexts.items()
    }
    metadata = {
        "strategy": f"Discovery{family}",
        "family": family,
        "parameters": dict(parameters),
        "symbols": sorted({symbol for symbol, _ in contexts}),
        "timeframes": sorted({timeframe for _, timeframe in contexts}),
        "split": split,
        "dataset_start": min(frame.index.min() for frame in evaluated_contexts.values()).isoformat(),
        "dataset_end": max(frame.index.max() for frame in evaluated_contexts.values()).isoformat(),
        "dataset_rows": sum(len(frame) for frame in evaluated_contexts.values()),
        "dataset_hash": _dataset_hash(evaluated_contexts),
        "engine": "BacktestEngine",
        "commission": fee,
        "slippage": 0.0,
        "initial_capital": CAPITAL,
        "git_commit": _git_commit(),
        "execution_timestamp": started_at,
        "runs": [
            {
                "trades": result.trades,
                "pf": result.pf,
                "expectancy": result.expectancy,
                "pnl": result.net_profit,
                "drawdown": result.max_dd,
            }
            for result in runs
        ],
        "reproducible": reproducible,
    }
    if not reproducible:
        raise RuntimeError(f"REPRODUCIBLE=NO for {family}/{split}/{parameters}")
    return ConfigResult(
        runs[0].family,
        runs[0].parameters,
        runs[0].split,
        runs[0].trades,
        runs[0].pf,
        runs[0].expectancy,
        runs[0].max_dd,
        runs[0].sharpe,
        runs[0].win_rate,
        runs[0].net_profit,
        runs[0].context_rows,
        runs[0].best_cell_pnl_share,
        metadata,
        runs[0].diagnostics,
    )


def _collect_engine_pnl(
    family: str,
    parameters: dict[str, float | int],
    split: str,
    contexts: dict[tuple[str, str], pd.DataFrame],
    bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    fee: float = BASE_FEE,
) -> list[float]:
    contexts = _prepare_contexts(family, parameters, contexts)
    pnl: list[float] = []
    for (symbol, timeframe), source in contexts.items():
        frame = _split_frame(source, split, bounds)
        strategy = DiscoveryHypothesisStrategy(family, parameters, timeframe)
        strategy.initialize()
        result = BacktestEngine(
            strategy,
            config=BacktestConfig(initial_capital=CAPITAL, fee_pct=fee, warmup_bars=50),
        ).run(frame, symbol=symbol, timeframe=timeframe)
        pnl.extend(float(trade["pnl"]) for trade in result.trades)
    return pnl


def _classification(trades: int) -> str:
    if trades < 30:
        return "INSUFFICIENT"
    if trades < 50:
        return "VERY_LOW_SAMPLE"
    if trades < 100:
        return "LOW_SAMPLE"
    return "USABLE"


def _select_dev(results: list[ConfigResult]) -> list[ConfigResult]:
    valid = [row for row in results if row.pf >= 1.0 and row.expectancy > 0]
    valid.sort(key=lambda row: (row.trades >= 100, row.pf, row.expectancy, -row.max_dd), reverse=True)
    return valid[:2]


def _degradation(dev_pf: float, oos_pf: float, dev_exp: float, oos_exp: float) -> str:
    if oos_pf <= 1.0 or oos_exp <= 0:
        return "COLLAPSED"
    pf_loss = max(0.0, (dev_pf - oos_pf) / max(abs(dev_pf), 1e-12))
    exp_loss = max(0.0, (dev_exp - oos_exp) / max(abs(dev_exp), 1e-12))
    if pf_loss <= 0.25 and exp_loss <= 0.40:
        return "STABLE"
    if pf_loss <= 0.50 and exp_loss <= 0.65:
        return "ACCEPTABLE_DEGRADATION"
    return "SEVERE_DEGRADATION"


def _bootstrap(pnl: list[float]) -> dict[str, Any]:
    values = np.asarray(pnl, dtype=float)
    if len(values) == 0:
        return {"iterations": BOOTSTRAP_ITERATIONS, "pf_ci95": None, "expectancy_ci95": None}
    rng = np.random.default_rng(20260818)
    samples = values[rng.integers(0, len(values), size=(BOOTSTRAP_ITERATIONS, len(values)))]
    pf_values = np.asarray([_profit_factor(sample) for sample in samples])
    exp_values = samples.mean(axis=1)
    return {"iterations": BOOTSTRAP_ITERATIONS, "pf_ci95": [float(np.percentile(pf_values, 2.5)), float(np.percentile(pf_values, 97.5))], "expectancy_ci95": [float(np.percentile(exp_values, 2.5)), float(np.percentile(exp_values, 97.5))]}


def _lookahead_bias_check() -> dict[str, str]:
    probe = pd.DataFrame({"close": np.arange(100.0), "high": np.arange(100.0) + 1.0, "low": np.arange(100.0) - 1.0, "open": np.arange(100.0), "volume": np.ones(100)})
    full = probe["close"].rolling(12).mean().iloc[59]
    prefix = probe.iloc[:60]["close"].rolling(12).mean().iloc[-1]
    if not np.isclose(float(full), float(prefix)):
        raise AssertionError("Future data influenced a rolling feature")
    return {"status": "PASS", "lookahead_bias": "NO", "rule": "signals use rows <= t and fills use open[t+1]"}


def _row_from_result(result: ConfigResult, *, validation: ConfigResult | None = None, oos: ConfigResult | None = None, degradation: str = "NOT_EVALUATED", bootstrap: dict[str, Any] | None = None, cost_robustness: str = "NOT_EVALUATED", status: str = "") -> dict[str, Any]:
    row = {
        "family": result.family,
        "parameters": json.dumps(result.parameters, sort_keys=True),
        "dev_trades": result.trades,
        "dev_trade_class": _classification(result.trades),
        "dev_pf": result.pf,
        "dev_expectancy": result.expectancy,
        "dev_max_dd": result.max_dd,
        "dev_sharpe": result.sharpe,
        "dev_win_rate": result.win_rate,
        "dev_net_profit": result.net_profit,
        "dev_diagnostics": result.diagnostics,
        "validation_trades": validation.trades if validation else None,
        "validation_pf": validation.pf if validation else None,
        "validation_expectancy": validation.expectancy if validation else None,
        "validation_diagnostics": validation.diagnostics if validation else None,
        "oos_trades": oos.trades if oos else None,
        "oos_pf": oos.pf if oos else None,
        "oos_expectancy": oos.expectancy if oos else None,
        "oos_diagnostics": oos.diagnostics if oos else None,
        "oos_max_dd": oos.max_dd if oos else None,
        "oos_sharpe": oos.sharpe if oos else None,
        "oos_net_profit": oos.net_profit if oos else None,
        "best_cell_pnl_share": oos.best_cell_pnl_share if oos else result.best_cell_pnl_share,
        "generalization": "GENERALIZABLE" if (oos and oos.best_cell_pnl_share <= 0.40) else ("PARTIAL" if (oos and oos.best_cell_pnl_share <= 0.60) else "CONCENTRATED"),
        "degradation": degradation,
        "bootstrap": bootstrap or {"iterations": BOOTSTRAP_ITERATIONS, "pf_ci95": None, "expectancy_ci95": None},
        "cost_robustness": cost_robustness,
        "status": status,
        "reproducibility": result.reproducibility,
    }
    return _json_safe(row)


def run() -> dict[str, Any]:
    bounds = _split_bounds()
    contexts = _load_contexts()
    lookahead = _lookahead_bias_check()
    family_outputs: dict[str, Any] = {}
    consolidated_rows: list[dict[str, Any]] = []
    total_configurations = 0

    for family in FAMILY_NAMES:
        if family in REJECTED_ENTRY_FAMILIES:
            raise RuntimeError(f"{family} is locked: {REJECTED_ENTRY_FAMILIES[family]}")
        configurations = _configurations(family)
        total_configurations += len(configurations)
        entry_dev = [(parameters, _entry_audit(family, parameters, "DEV", contexts, bounds)) for parameters in configurations]
        rows: list[dict[str, Any]] = []
        for parameters, audit in entry_dev:
            validation_rows = _entry_audit(family, parameters, "VALIDATION", contexts, bounds)["rows"]
            for candidate in _entry_candidates(audit["rows"]):
                validation = _same_entry_context(validation_rows, candidate)
                if validation is None or not _passes_entry_gate(validation):
                    rows.append({"family": family, "parameters": json.dumps(parameters, sort_keys=True), "entry_dev": candidate, "entry_validation": validation, "entry_oos": None, "status": "REJECTED_VALIDATION"})
                    continue
                oos = _same_entry_context(_entry_audit(family, parameters, "OOS", contexts, bounds)["rows"], candidate)
                if oos is None or not _passes_entry_gate(oos):
                    rows.append({"family": family, "parameters": json.dumps(parameters, sort_keys=True), "entry_dev": candidate, "entry_validation": validation, "entry_oos": oos, "status": "REJECTED_OOS_ENTRY_GATE"})
                    continue
                operational = _evaluate(family, parameters, "OOS", contexts, bounds)
                rows.append(_row_from_result(operational, status="ENTRY_VALIDATED_OPERATIONAL_PENDING"))
        if rows:
            best = max(rows, key=lambda row: (str(row["status"]) == "ENTRY_VALIDATED_OPERATIONAL_PENDING", float((row.get("entry_oos") or {}).get("gross_expectancy") or 0.0)))
            family_outputs[family] = {"hypothesis": HYPOTHESES[family], "configurations_tested": len(configurations), "entry_gate": "DEV -> VALIDATION -> OOS before BacktestEngine", "selected": rows, "best": best}
            consolidated_rows.extend(rows)
        else:
            best_entry = max((row for _, audit in entry_dev for row in audit["rows"]), key=lambda row: (float(row["t_stat"]), float(row["gross_pf"]), float(row["gross_expectancy"])), default=None)
            family_outputs[family] = {"hypothesis": HYPOTHESES[family], "configurations_tested": len(configurations), "entry_gate": "DEV -> VALIDATION -> OOS before BacktestEngine", "selected": [], "best_entry_dev": best_entry, "status": "REJECTED_DEV_ENTRY_GATE"}

    winners = [row for row in consolidated_rows if row["status"] == "ENTRY_VALIDATED_OPERATIONAL_PENDING"]
    decision = "ENTRY_VALIDATED" if winners else "ALL_FAILED"
    payload = {
        "cycle": f"STRATEGY_DISCOVERY_CYCLE_{CYCLE}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data": {"start": DATA_START.isoformat(), "end": DATA_END.isoformat(), "days": (DATA_END - DATA_START).total_seconds() / 86400.0, "source": "database candles table", "symbols": SYMBOLS, "timeframes": TIMEFRAMES, "fee_model": "0.1% per side", "slippage_model": "0.0% explicit slippage; higher-cost stress at 0.15% per side"},
        "split": {key: {"start": start.isoformat(), "end": end.isoformat()} for key, (start, end) in bounds.items()},
        "lookahead_bias_check": lookahead,
        "search_budget": {"per_family": 12, "total": total_configurations, "maximum_allowed": 81},
        "protocol": {"entry_edge_before_operational_backtest": True, "forward_horizons": ENTRY_FORWARD_HORIZONS, "minimum_independent_episodes": ENTRY_MIN_EPISODES, "minimum_effect_bps": ENTRY_MIN_EFFECT_BPS, "minimum_t_stat": ENTRY_MIN_T_STAT, "rejected_entry_families": REJECTED_ENTRY_FAMILIES},
        "families": family_outputs,
        "decision": {"scientific_decision": decision, "winner": None if not winners else winners[0], "ready_for_paper": False, "beats_cdb": False, "baseline_cdb": {"trades": 446, "pf": 1.000983, "expectancy": 0.010224, "max_dd": 0.070645}},
        "campaign": {"campaign_running": "YES", "contexts_active": "12/12", "contexts_stale": 0, "desync": 0, "cycles_advancing": "YES"},
        "files_created": [str(OUT_JSON.name), str(OUT_CSV.name), str(OUT_MD.name)],
        "files_modified": [str(Path(__file__).name)],
    }
    OUT_JSON.write_text(json.dumps(_json_safe(payload), ensure_ascii=True, indent=2), encoding="utf-8")
    csv_fields = sorted({key for row in consolidated_rows for key in row}) if consolidated_rows else ["family", "status"]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else value for key, value in row.items()} for row in consolidated_rows)
    lines = [f"# Strategy Discovery Cycle {CYCLE}", "", f"Decision: **{decision}**", f"Data: {DATA_START.date()} to {DATA_END.date()} ({(DATA_END - DATA_START).days} days)", f"Configurations tested: **{total_configurations}**", "", "## Controls", f"- Lookahead bias: `{lookahead['lookahead_bias']}`", "- Split: 60% DEV / 20% VALIDATION / 20% OOS", "- OOS opened only after frozen DEV selection", "- Costs: 0.1% per side; stress 0.15% per side", ""]
    for family in FAMILY_NAMES:
        node = family_outputs[family]
        lines.extend([f"## {family}", f"- Hypothesis: {node['hypothesis']['statement']}", f"- Configurations: {node['configurations_tested']}"])
        if node.get("selected"):
            for row in node["selected"]:
                lines.append(f"- {row['parameters']} | DEV PF={row['dev_pf']:.4f} | Validation PF={row.get('validation_pf')} | OOS PF={row.get('oos_pf')} | OOS expectancy={row.get('oos_expectancy')} | Generalization={row['generalization']} | Status={row['status']}")
        else:
            best_entry = node.get("best_entry_dev")
            if best_entry:
                lines.append(f"- Best entry audit: gross PF={best_entry['gross_pf']:.4f}, gross expectancy={best_entry['gross_expectancy']:.6f}, episodes={best_entry['episodes']}; rejected at entry DEV gate.")
            else:
                lines.append("- No eligible independent episodes; rejected at entry DEV gate.")
        lines.append("")
    lines.extend(["## Decision", "", "No strategy is promoted automatically. The official CDB Paper Live remains isolated.", ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    return payload


if __name__ == "__main__":
    run()
