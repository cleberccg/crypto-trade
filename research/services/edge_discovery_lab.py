from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

import performance_analytics
import robustness_analytics
from backtesting.engine import BacktestConfig, BacktestEngine
from backtesting.metrics import compute_metrics
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.factory import create_strategy

RESULTS_DIR = Path(__file__).resolve().parents[2] / "optimization" / "results"
REGIME_CACHE_DIR = RESULTS_DIR / "edge_regimes"

TREND_REGIMES = ("bullish", "bearish", "sideways")
VOL_REGIMES = ("high_volatility", "low_volatility")


@dataclass(frozen=True)
class EdgeDiscoveryConfig:
    report_file: str | None = None
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    timeframes: tuple[str, ...] = ("5m", "15m", "1h")
    window_days: int = 120
    capital: float = 10_000.0
    max_bars: int = 3500
    min_trades_per_context: int = 5
    only_paper_candidates: bool = True
    limit_candidates: int = 0
    output_prefix: str = "edge_discovery_lab"


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 6) if values else None


def _slug_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _context_score(row: dict[str, Any]) -> float:
    pf = float(row.get("profit_factor") or 0.0)
    sharpe = float(row.get("sharpe") or 0.0)
    expectancy = float(row.get("expectancy") or 0.0)
    drawdown = float(row.get("drawdown_pct") or 1.0)
    trades = float(row.get("number_of_trades") or 0.0)
    coverage = min(1.0, trades / 40.0)
    score = (
        0.35 * min(2.0, max(0.0, pf)) / 2.0
        + 0.25 * min(2.0, max(-1.0, sharpe + 1.0)) / 2.0
        + 0.20 * min(1.0, max(0.0, expectancy + 0.5))
        + 0.10 * max(0.0, 1.0 - min(1.0, drawdown / 0.30))
        + 0.10 * coverage
    )
    return round(score * 100.0, 4)


def classify_market_regimes(candles: pd.DataFrame) -> pd.DataFrame:
    df = candles.copy()
    if df.empty:
        return df

    df = df.sort_index().copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")

    df["ema_fast"] = close.ewm(span=20, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=50, adjust=False).mean()
    df["trend_score"] = (df["ema_fast"] - df["ema_slow"]) / close.replace(0, pd.NA)

    abs_trend = pd.to_numeric(df["trend_score"], errors="coerce").abs()
    strong_thr = float(abs_trend.quantile(0.80)) if abs_trend.notna().any() else 0.0012
    moderate_thr = float(abs_trend.quantile(0.55)) if abs_trend.notna().any() else 0.0006
    strong_thr = max(strong_thr, 0.0012)
    moderate_thr = max(moderate_thr, 0.0006)

    df["trend_regime_detail"] = "consolidacao"
    df.loc[df["trend_score"] >= moderate_thr, "trend_regime_detail"] = "tendencia_moderada_alta"
    df.loc[df["trend_score"] >= strong_thr, "trend_regime_detail"] = "tendencia_forte_alta"
    df.loc[df["trend_score"] <= -moderate_thr, "trend_regime_detail"] = "tendencia_moderada_baixa"
    df.loc[df["trend_score"] <= -strong_thr, "trend_regime_detail"] = "tendencia_forte_baixa"

    df["trend_bucket"] = "sideways"
    df.loc[df["trend_score"] >= moderate_thr, "trend_bucket"] = "bullish"
    df.loc[df["trend_score"] <= -moderate_thr, "trend_bucket"] = "bearish"

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=8).mean()
    df["atr_pct"] = df["atr14"] / close.replace(0, pd.NA)

    low_vol_thr = float(df["atr_pct"].quantile(0.30)) if df["atr_pct"].notna().any() else 0.0
    high_vol_thr = float(df["atr_pct"].quantile(0.70)) if df["atr_pct"].notna().any() else 0.0
    df["vol_regime"] = "normal_volatility"
    df.loc[df["atr_pct"] <= low_vol_thr, "vol_regime"] = "low_volatility"
    df.loc[df["atr_pct"] >= high_vol_thr, "vol_regime"] = "high_volatility"

    return df


def attach_trade_regimes(trades: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()

    left = trades.copy().sort_values("entry_time")
    right = regimes.copy().sort_index()

    left["entry_time"] = pd.to_datetime(left["entry_time"], utc=True)
    left["exit_time"] = pd.to_datetime(left["exit_time"], utc=True)
    left = left.sort_values("entry_time")

    right = right.reset_index().rename(columns={right.index.name or "index": "timestamp"})
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True)
    cols = ["timestamp", "trend_bucket", "trend_regime_detail", "vol_regime", "trend_score", "atr_pct"]

    merged = pd.merge_asof(
        left,
        right[cols].sort_values("timestamp"),
        left_on="entry_time",
        right_on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta("7D"),
    )
    return merged.drop(columns=["timestamp"])


def metrics_from_trades(trades: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    if trades.empty:
        return {
            "number_of_trades": 0,
            "profit_factor": None,
            "sharpe": None,
            "expectancy": None,
            "drawdown_pct": None,
            "win_rate": None,
            "return_pct": None,
            "net_profit": 0.0,
        }

    ordered = trades.copy().sort_values("exit_time")
    ordered["pnl"] = pd.to_numeric(ordered["pnl"], errors="coerce").fillna(0.0)
    exit_times = pd.to_datetime(ordered["exit_time"], utc=True)
    equity = pd.Series(initial_capital + ordered["pnl"].cumsum().to_numpy(), index=exit_times)
    metrics = compute_metrics(ordered.to_dict("records"), equity, initial_capital)
    return {
        "number_of_trades": int(metrics.total_trades),
        "profit_factor": float(metrics.profit_factor),
        "sharpe": float(metrics.sharpe_ratio),
        "expectancy": float(metrics.expectancy),
        "drawdown_pct": float(metrics.max_drawdown_pct),
        "win_rate": float(metrics.win_rate),
        "return_pct": float(metrics.return_pct),
        "net_profit": float(metrics.net_profit),
    }


def build_scientific_ranking(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in rows:
        robustness = float(row.get("robustness_score") or 0.0)
        consistency = float(row.get("consistency_score") or 0.0)
        asset_robustness = float(row.get("asset_robustness") or 0.0)
        timeframe_robustness = float(row.get("timeframe_robustness") or 0.0)
        regime_robustness = float(row.get("regime_robustness") or 0.0)
        avg_pf = float(row.get("profit_factor_mean") or 0.0)
        max_dd = float(row.get("drawdown_max") or 1.0)

        evidence_score = 100.0 * (
            0.25 * (robustness / 100.0)
            + 0.20 * (consistency / 100.0)
            + 0.20 * asset_robustness
            + 0.15 * timeframe_robustness
            + 0.15 * regime_robustness
            + 0.05 * max(0.0, 1.0 - min(1.0, max_dd / 0.25))
        )

        evidence_ready = (
            robustness >= 65.0
            and consistency >= 65.0
            and avg_pf >= 1.0
            and asset_robustness >= 0.50
            and timeframe_robustness >= 0.50
            and regime_robustness >= 0.40
        )

        ranked.append(
            {
                **row,
                "evidence_score": round(evidence_score, 4),
                "ready_for_prolonged_paper": evidence_ready,
            }
        )

    ranked.sort(
        key=lambda item: (
            not bool(item.get("ready_for_prolonged_paper")),
            -float(item.get("evidence_score") or 0.0),
            -float(item.get("robustness_score") or 0.0),
            -float(item.get("consistency_score") or 0.0),
        )
    )
    for idx, row in enumerate(ranked, start=1):
        row["paper_ranking"] = idx
    return ranked


class EdgeDiscoveryLabService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        REGIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: EdgeDiscoveryConfig) -> dict[str, Any]:
        campaigns = robustness_analytics._load_phase13_campaigns()
        if not campaigns:
            raise RuntimeError("Nenhuma campanha da FASE 13 encontrada para edge discovery.")

        reference_report = self._load_reference_report(cfg, campaigns)
        backlog = reference_report.get("backlog", []) if isinstance(reference_report.get("backlog"), list) else []
        candidates = self._select_candidates(backlog, cfg)
        if not candidates:
            raise RuntimeError("Nenhum candidato elegivel encontrado no backlog da FASE 13.")

        performance_rows = performance_analytics._build_strategy_history(performance_analytics._load_phase13_campaigns()).get("rows", [])
        robustness_rows = robustness_analytics._build_strategy_robustness(campaigns).get("rows", [])
        perf_map = {str(row.get("strategy")): row for row in performance_rows if isinstance(row, dict)}
        robust_map = {str(row.get("strategy")): row for row in robustness_rows if isinstance(row, dict)}

        profiles: list[dict[str, Any]] = []
        matrix_rows: list[dict[str, Any]] = []

        for item in candidates:
            analyzed = self._analyze_candidate(item, cfg, perf_map, robust_map, campaigns)
            profiles.append(analyzed["profile"])
            matrix_rows.extend(analyzed["matrix_rows"])

        ranked_profiles = build_scientific_ranking(profiles)
        report = self._build_report(reference_report, ranked_profiles, matrix_rows)
        outputs = self._write_outputs(cfg.output_prefix, report, ranked_profiles, matrix_rows)
        return {
            "summary": report.get("decision", {}),
            "report": report,
            "outputs": outputs,
        }

    def _load_reference_report(
        self,
        cfg: EdgeDiscoveryConfig,
        campaigns: list[robustness_analytics.Campaign],
    ) -> dict[str, Any]:
        if cfg.report_file:
            payload = json.loads(Path(cfg.report_file).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Arquivo de campanha invalido para edge discovery.")
            return payload
        return campaigns[-1].payload

    def _select_candidates(self, backlog: list[dict[str, Any]], cfg: EdgeDiscoveryConfig) -> list[dict[str, Any]]:
        rows = [row for row in backlog if isinstance(row, dict)]
        if cfg.only_paper_candidates:
            rows = [row for row in rows if str(row.get("state")) == "PAPER_CANDIDATE"]

        rows = sorted(
            rows,
            key=lambda row: (
                -float(row.get("queue_score") or 0.0),
                str(row.get("candidate_name") or ""),
            ),
        )
        if cfg.limit_candidates > 0:
            rows = rows[: cfg.limit_candidates]
        return rows

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

    def _load_or_build_regimes(self, symbol: str, timeframe: str, candles: pd.DataFrame) -> pd.DataFrame:
        cache_path = REGIME_CACHE_DIR / f"{_slug_symbol(symbol)}__{timeframe}.csv"
        if cache_path.exists():
            try:
                cached = pd.read_csv(cache_path)
                cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
                cached = cached.set_index("timestamp").sort_index()
                if len(cached) == len(candles) and not candles.empty:
                    if cached.index.max() == candles.index.max() and cached.index.min() == candles.index.min():
                        return cached
            except Exception:
                pass

        classified = classify_market_regimes(candles)
        to_save = classified.reset_index().rename(columns={classified.index.name or "index": "timestamp"})
        to_save.to_csv(cache_path, index=False)
        return classified

    def _run_backtest(self, strategy_name: str, symbol: str, timeframe: str, candles: pd.DataFrame, capital: float) -> dict[str, Any]:
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        result = BacktestEngine(strategy, config=BacktestConfig(initial_capital=capital)).run(
            candles,
            symbol=symbol,
            timeframe=timeframe,
        )
        metrics = result.metrics
        return {
            "trades": pd.DataFrame(result.trades),
            "metrics": {
                "number_of_trades": int(metrics.total_trades),
                "profit_factor": float(metrics.profit_factor),
                "sharpe": float(metrics.sharpe_ratio),
                "expectancy": float(metrics.expectancy),
                "drawdown_pct": float(metrics.max_drawdown_pct),
                "win_rate": float(metrics.win_rate),
                "return_pct": float(metrics.return_pct),
                "net_profit": float(metrics.net_profit),
            },
        }

    def _analyze_candidate(
        self,
        item: dict[str, Any],
        cfg: EdgeDiscoveryConfig,
        perf_map: dict[str, dict[str, Any]],
        robust_map: dict[str, dict[str, Any]],
        campaigns: list[robustness_analytics.Campaign],
    ) -> dict[str, Any]:
        name = str(item.get("candidate_name") or "").strip()
        family = str(item.get("family") or "unknown")
        strategy_name = str(item.get("platform_strategy_name") or name).strip()
        if not strategy_name:
            raise RuntimeError(f"Candidata sem platform_strategy_name: {name}")

        context_rows: list[dict[str, Any]] = []
        regime_rows: list[dict[str, Any]] = []

        for symbol in cfg.symbols:
            for timeframe in cfg.timeframes:
                candles = self._load_market_data(symbol, timeframe, cfg.window_days, cfg.max_bars)
                if candles.empty:
                    context_rows.append(
                        {
                            "strategy": name,
                            "platform_strategy_name": strategy_name,
                            "family": family,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "regime_type": "full",
                            "regime": "all",
                            "status": "no_data",
                            "number_of_trades": 0,
                            "profit_factor": None,
                            "sharpe": None,
                            "expectancy": None,
                            "drawdown_pct": None,
                            "return_pct": None,
                            "context_score": 0.0,
                        }
                    )
                    continue

                regimes = self._load_or_build_regimes(symbol, timeframe, candles)
                backtest = self._run_backtest(strategy_name, symbol, timeframe, candles, cfg.capital)
                trades = backtest["trades"]
                trade_regimes = attach_trade_regimes(trades, regimes) if not trades.empty else trades.copy()
                full_metrics = dict(backtest["metrics"])
                full_row = {
                    "strategy": name,
                    "platform_strategy_name": strategy_name,
                    "family": family,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "regime_type": "full",
                    "regime": "all",
                    "status": "ok",
                    **full_metrics,
                }
                full_row["context_score"] = _context_score(full_row)
                context_rows.append(full_row)

                for trend_regime in TREND_REGIMES:
                    subset = trade_regimes[trade_regimes["trend_bucket"] == trend_regime] if not trade_regimes.empty else trade_regimes
                    metrics = metrics_from_trades(subset, cfg.capital)
                    row = {
                        "strategy": name,
                        "platform_strategy_name": strategy_name,
                        "family": family,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "regime_type": "trend",
                        "regime": trend_regime,
                        "status": "ok",
                        **metrics,
                    }
                    row["context_score"] = _context_score(row)
                    regime_rows.append(row)

                for vol_regime in VOL_REGIMES:
                    subset = trade_regimes[trade_regimes["vol_regime"] == vol_regime] if not trade_regimes.empty else trade_regimes
                    metrics = metrics_from_trades(subset, cfg.capital)
                    row = {
                        "strategy": name,
                        "platform_strategy_name": strategy_name,
                        "family": family,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "regime_type": "volatility",
                        "regime": vol_regime,
                        "status": "ok",
                        **metrics,
                    }
                    row["context_score"] = _context_score(row)
                    regime_rows.append(row)

        matrix_rows = context_rows + regime_rows
        profile = self._build_profile(name, family, perf_map.get(name, {}), robust_map.get(name, {}), matrix_rows, campaigns, cfg)
        return {
            "profile": profile,
            "matrix_rows": matrix_rows,
        }

    def _current_paper_candidate_streak(self, campaigns: list[robustness_analytics.Campaign], strategy_name: str) -> int:
        streak = 0
        for campaign in reversed(campaigns):
            backlog = campaign.payload.get("backlog", []) if isinstance(campaign.payload.get("backlog"), list) else []
            matching = [row for row in backlog if isinstance(row, dict) and str(row.get("candidate_name")) == strategy_name]
            if not matching:
                break
            state = str(matching[0].get("state") or "")
            if state == "PAPER_CANDIDATE":
                streak += 1
                continue
            break
        return streak

    def _build_profile(
        self,
        name: str,
        family: str,
        perf_row: dict[str, Any],
        robust_row: dict[str, Any],
        matrix_rows: list[dict[str, Any]],
        campaigns: list[robustness_analytics.Campaign],
        cfg: EdgeDiscoveryConfig,
    ) -> dict[str, Any]:
        full_rows = [
            row for row in matrix_rows
            if row.get("regime_type") == "full" and int(row.get("number_of_trades") or 0) >= cfg.min_trades_per_context
        ]
        trend_rows = [
            row for row in matrix_rows
            if row.get("regime_type") == "trend" and int(row.get("number_of_trades") or 0) >= cfg.min_trades_per_context
        ]
        vol_rows = [
            row for row in matrix_rows
            if row.get("regime_type") == "volatility" and int(row.get("number_of_trades") or 0) >= cfg.min_trades_per_context
        ]

        best_context = max(full_rows, key=lambda row: float(row.get("context_score") or 0.0), default=None)
        best_trend = max(trend_rows, key=lambda row: float(row.get("context_score") or 0.0), default=None)
        best_vol = max(vol_rows, key=lambda row: float(row.get("context_score") or 0.0), default=None)

        asset_groups: dict[str, list[dict[str, Any]]] = {}
        timeframe_groups: dict[str, list[dict[str, Any]]] = {}
        for row in full_rows:
            asset_groups.setdefault(str(row.get("symbol")), []).append(row)
            timeframe_groups.setdefault(str(row.get("timeframe")), []).append(row)

        asset_scores = {
            asset: _avg([float(r.get("context_score") or 0.0) for r in rows]) or 0.0
            for asset, rows in asset_groups.items()
        }
        timeframe_scores = {
            timeframe: _avg([float(r.get("context_score") or 0.0) for r in rows]) or 0.0
            for timeframe, rows in timeframe_groups.items()
        }

        positive_assets = sum(1 for rows in asset_groups.values() if any(float(r.get("profit_factor") or 0.0) >= 1.0 for r in rows))
        positive_timeframes = sum(1 for rows in timeframe_groups.values() if any(float(r.get("profit_factor") or 0.0) >= 1.0 for r in rows))
        positive_trend_regimes = len({str(r.get("regime")) for r in trend_rows if float(r.get("profit_factor") or 0.0) >= 1.0})
        positive_vol_regimes = len({str(r.get("regime")) for r in vol_rows if float(r.get("profit_factor") or 0.0) >= 1.0})

        asset_robustness = round(positive_assets / max(1, len(asset_groups)), 6) if asset_groups else 0.0
        timeframe_robustness = round(positive_timeframes / max(1, len(timeframe_groups)), 6) if timeframe_groups else 0.0
        regime_robustness = round((positive_trend_regimes + positive_vol_regimes) / 5.0, 6)

        ideal_parts = []
        if best_context:
            ideal_parts.append(f"ativo {best_context['symbol']}")
            ideal_parts.append(f"timeframe {best_context['timeframe']}")
        if best_trend:
            ideal_parts.append(f"regime {best_trend['regime']}")
        if best_vol:
            ideal_parts.append(f"volatilidade {best_vol['regime']}")

        return {
            "name": name,
            "family": family,
            "consistency_score": float(perf_row.get("consistency_score") or robust_row.get("consistency_score") or 0.0),
            "robustness_score": float(robust_row.get("robustness_score") or 0.0),
            "profit_factor_mean": _safe_float(robust_row.get("mean_profit_factor") or perf_row.get("mean_profit_factor")),
            "sharpe_mean": _safe_float(robust_row.get("mean_sharpe") or perf_row.get("mean_sharpe")),
            "expectancy_mean": _safe_float(robust_row.get("mean_expectancy") or perf_row.get("mean_expectancy")),
            "drawdown_mean": _safe_float(robust_row.get("mean_drawdown") or perf_row.get("mean_drawdown")),
            "drawdown_max": _safe_float(robust_row.get("max_drawdown")),
            "number_of_campaigns": int(robust_row.get("number_of_campaigns") or perf_row.get("number_of_campaigns") or 0),
            "paper_candidate_streak": self._current_paper_candidate_streak(campaigns, name),
            "baseline_superou_count": int(robust_row.get("baseline_superou_count") or perf_row.get("baseline_superou_count") or 0),
            "trend": str(robust_row.get("trend") or "Stable"),
            "asset_robustness": asset_robustness,
            "timeframe_robustness": timeframe_robustness,
            "regime_robustness": regime_robustness,
            "best_asset": max(asset_scores, key=asset_scores.get) if asset_scores else None,
            "best_timeframe": max(timeframe_scores, key=timeframe_scores.get) if timeframe_scores else None,
            "ideal_environment": ", ".join(ideal_parts) if ideal_parts else "insufficient_evidence",
            "depends_on_asset": bool(asset_groups) and asset_robustness < 0.5,
            "contexts_tested": len(full_rows),
            "regime_rows_tested": len(trend_rows) + len(vol_rows),
        }

    def _best_strategy_for_regime(self, rows: list[dict[str, Any]], regime_type: str, regime: str) -> dict[str, Any] | None:
        candidates = [
            row for row in rows
            if row.get("regime_type") == regime_type and row.get("regime") == regime and int(row.get("number_of_trades") or 0) > 0
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: float(row.get("context_score") or 0.0))

    def _build_report(
        self,
        reference_report: dict[str, Any],
        profiles: list[dict[str, Any]],
        matrix_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        best_robust = max(profiles, key=lambda row: float(row.get("robustness_score") or 0.0), default=None)
        best_consistent = max(profiles, key=lambda row: float(row.get("consistency_score") or 0.0), default=None)
        best_pf = max(profiles, key=lambda row: float(row.get("profit_factor_mean") or 0.0), default=None)
        best_dd = min(
            [row for row in profiles if row.get("drawdown_mean") is not None],
            key=lambda row: float(row.get("drawdown_mean") or 999.0),
            default=None,
        )

        timeframe_rows = [row for row in matrix_rows if row.get("regime_type") == "full" and int(row.get("number_of_trades") or 0) > 0]
        timeframe_scores: dict[str, list[float]] = {}
        for row in timeframe_rows:
            timeframe_scores.setdefault(str(row.get("timeframe")), []).append(float(row.get("context_score") or 0.0))
        best_timeframe = None
        if timeframe_scores:
            best_timeframe = max(timeframe_scores, key=lambda key: _avg(timeframe_scores[key]) or 0.0)

        best_bull = self._best_strategy_for_regime(matrix_rows, "trend", "bullish")
        best_bear = self._best_strategy_for_regime(matrix_rows, "trend", "bearish")
        best_sideways = self._best_strategy_for_regime(matrix_rows, "trend", "sideways")
        best_high_vol = self._best_strategy_for_regime(matrix_rows, "volatility", "high_volatility")
        best_low_vol = self._best_strategy_for_regime(matrix_rows, "volatility", "low_volatility")

        ready = [row for row in profiles if bool(row.get("ready_for_prolonged_paper"))]
        asset_specific = [row for row in profiles if bool(row.get("depends_on_asset"))]

        decision = {
            "exists_strategy_ready_for_prolonged_paper": bool(ready),
            "ready_count": len(ready),
            "ranking": [
                {
                    "rank": int(row.get("paper_ranking") or 0),
                    "strategy": row.get("name"),
                    "evidence_score": row.get("evidence_score"),
                    "robustness_score": row.get("robustness_score"),
                    "consistency_score": row.get("consistency_score"),
                    "asset_robustness": row.get("asset_robustness"),
                    "timeframe_robustness": row.get("timeframe_robustness"),
                    "regime_robustness": row.get("regime_robustness"),
                }
                for row in ready or profiles[:5]
            ],
            "missing_evidence": [] if ready else [
                "robustez cruzada insuficiente entre ativos/timeframes/regimes",
                "amostra historica ainda concentrada em poucos contextos positivos",
            ],
        }

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "source_campaign": {
                "run_id": reference_report.get("run_id"),
                "generated_at": reference_report.get("generated_at"),
                "symbol": reference_report.get("symbol"),
                "timeframe": reference_report.get("timeframe"),
                "paper_candidate_count": reference_report.get("paper_candidate_count"),
            },
            "profiles": profiles,
            "edge_matrix": matrix_rows,
            "final_report": {
                "strategies": {
                    "highest_robustness": best_robust.get("name") if best_robust else None,
                    "highest_consistency": best_consistent.get("name") if best_consistent else None,
                    "highest_profit_factor_mean": best_pf.get("name") if best_pf else None,
                    "lowest_drawdown": best_dd.get("name") if best_dd else None,
                },
                "regimes": {
                    "bullish": best_bull.get("strategy") if best_bull else None,
                    "bearish": best_bear.get("strategy") if best_bear else None,
                    "sideways": best_sideways.get("strategy") if best_sideways else None,
                    "high_volatility": best_high_vol.get("strategy") if best_high_vol else None,
                    "low_volatility": best_low_vol.get("strategy") if best_low_vol else None,
                },
                "timeframes": {
                    "most_robust_timeframe": best_timeframe,
                },
                "assets": {
                    "strategies_specific_to_btc": [row.get("name") for row in asset_specific],
                    "strategies_with_cross_asset_evidence": [row.get("name") for row in profiles if not bool(row.get("depends_on_asset"))],
                },
            },
            "decision": decision,
        }

    def _write_outputs(
        self,
        output_prefix: str,
        report: dict[str, Any],
        profiles: list[dict[str, Any]],
        matrix_rows: list[dict[str, Any]],
    ) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        matrix_csv = self._results_dir / f"{output_prefix}_{stamp}_matrix.csv"
        profiles_csv = self._results_dir / f"{output_prefix}_{stamp}_profiles.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._render_markdown(report), encoding="utf-8")
        self._write_csv(matrix_csv, matrix_rows)
        self._write_csv(profiles_csv, profiles)

        return {
            "json": str(json_path),
            "md": str(md_path),
            "matrix_csv": str(matrix_csv),
            "profiles_csv": str(profiles_csv),
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
        final = report.get("final_report", {}) if isinstance(report.get("final_report"), dict) else {}
        strategies = final.get("strategies", {}) if isinstance(final.get("strategies"), dict) else {}
        regimes = final.get("regimes", {}) if isinstance(final.get("regimes"), dict) else {}
        timeframes = final.get("timeframes", {}) if isinstance(final.get("timeframes"), dict) else {}
        assets = final.get("assets", {}) if isinstance(final.get("assets"), dict) else {}
        decision = report.get("decision", {}) if isinstance(report.get("decision"), dict) else {}

        lines = [
            "# FASE 17 - Edge Discovery Lab",
            "",
            "## Estrategias",
            f"- Maior Robustness Score: {strategies.get('highest_robustness')}",
            f"- Maior Consistency Score: {strategies.get('highest_consistency')}",
            f"- Maior PF medio: {strategies.get('highest_profit_factor_mean')}",
            f"- Menor Drawdown: {strategies.get('lowest_drawdown')}",
            "",
            "## Regimes",
            f"- Tendencia de Alta: {regimes.get('bullish')}",
            f"- Tendencia de Baixa: {regimes.get('bearish')}",
            f"- Mercado Lateral: {regimes.get('sideways')}",
            f"- Alta Volatilidade: {regimes.get('high_volatility')}",
            f"- Baixa Volatilidade: {regimes.get('low_volatility')}",
            "",
            "## Timeframes",
            f"- Timeframe com maior robustez: {timeframes.get('most_robust_timeframe')}",
            "",
            "## Ativos",
            f"- Estrategias especificas de BTC: {', '.join(assets.get('strategies_specific_to_btc', [])) or 'nenhuma'}",
            f"- Estrategias com evidencia cross-asset: {', '.join(assets.get('strategies_with_cross_asset_evidence', [])) or 'nenhuma'}",
            "",
            "## Decisao",
            f"- Existe estrategia pronta para Paper Trading prolongado: {'SIM' if decision.get('exists_strategy_ready_for_prolonged_paper') else 'NAO'}",
        ]

        ranking = decision.get("ranking", []) if isinstance(decision.get("ranking"), list) else []
        if ranking:
            lines.append("")
            lines.append("## Ranking")
            for row in ranking:
                lines.append(
                    f"- #{row.get('rank')} {row.get('strategy')} | evidence={row.get('evidence_score')} | robust={row.get('robustness_score')} | consistency={row.get('consistency_score')}"
                )

        missing = decision.get("missing_evidence", []) if isinstance(decision.get("missing_evidence"), list) else []
        if missing:
            lines.append("")
            lines.append("## Evidencias Faltantes")
            for item in missing:
                lines.append(f"- {item}")

        return "\n".join(lines) + "\n"