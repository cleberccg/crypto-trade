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

import robustness_analytics
from backtesting.engine import BacktestConfig, BacktestEngine
from backtesting.metrics import compute_metrics
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.factory import create_strategy

RESULTS_DIR = Path(__file__).resolve().parents[2] / "optimization" / "results"
PHASE18_DIR = RESULTS_DIR / "market_regime_router"
REGIME_CACHE_DIR = PHASE18_DIR / "regime_cache"

TREND_REGIMES = ("bullish", "bearish", "sideways")
VOL_REGIMES = ("high_volatility", "normal_volatility", "low_volatility")


@dataclass(frozen=True)
class MarketRegimeRouterConfig:
    report_file: str | None = None
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    timeframes: tuple[str, ...] = ("5m", "15m", "1h")
    window_days: int = 120
    capital: float = 10_000.0
    max_bars: int = 3500
    min_trades_per_regime: int = 5
    only_paper_candidates: bool = True
    limit_candidates: int = 0
    baseline_strategy: str | None = None
    output_prefix: str = "phase18_market_regime_router"


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
    """Classify bars in trend and volatility regimes.

    The classifier is deterministic and can be safely cached for re-use in
    future campaigns.
    """
    if candles.empty:
        return candles.copy()

    df = candles.sort_index().copy()
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

    df["regime_key"] = df["trend_bucket"].astype(str) + "|" + df["vol_regime"].astype(str)
    return df


def attach_trade_regimes(trades: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()

    left = trades.copy().sort_values("entry_time")
    left["entry_time"] = pd.to_datetime(left["entry_time"], utc=True)
    left["exit_time"] = pd.to_datetime(left["exit_time"], utc=True)

    right = regimes.copy().sort_index().reset_index()
    right = right.rename(columns={right.columns[0]: "timestamp"})
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True)

    cols = ["timestamp", "trend_bucket", "vol_regime", "regime_key", "trend_score", "atr_pct"]
    merged = pd.merge_asof(
        left.sort_values("entry_time"),
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
            "return_pct": None,
            "net_profit": 0.0,
            "win_rate": None,
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
        "return_pct": float(metrics.return_pct),
        "net_profit": float(metrics.net_profit),
        "win_rate": float(metrics.win_rate),
    }


def build_router_map(regime_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in regime_rows:
        key = (
            str(row.get("symbol") or "*"),
            str(row.get("timeframe") or "*"),
            str(row.get("trend_bucket") or "sideways"),
            str(row.get("vol_regime") or "normal_volatility"),
        )
        grouped.setdefault(key, []).append(row)

    mapping: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        ranked = sorted(
            rows,
            key=lambda item: (
                -float(item.get("context_score") or 0.0),
                -float(item.get("number_of_trades") or 0.0),
                str(item.get("strategy") or ""),
            ),
        )
        best = ranked[0]
        mapping.append(
            {
                "symbol": key[0],
                "timeframe": key[1],
                "trend_bucket": key[2],
                "vol_regime": key[3],
                "recommended_strategy": str(best.get("strategy") or ""),
                "recommended_platform_strategy": str(best.get("platform_strategy_name") or ""),
                "score": float(best.get("context_score") or 0.0),
                "trades_support": int(best.get("number_of_trades") or 0),
            }
        )

    return sorted(
        mapping,
        key=lambda x: (
            str(x.get("symbol") or ""),
            str(x.get("timeframe") or ""),
            str(x.get("trend_bucket") or ""),
            str(x.get("vol_regime") or ""),
        ),
    )


def evaluate_hypothesis(single_metrics: dict[str, Any], router_metrics: dict[str, Any], robustness_rows: list[dict[str, Any]]) -> dict[str, Any]:
    pf_delta = float(router_metrics.get("profit_factor") or 0.0) - float(single_metrics.get("profit_factor") or 0.0)
    sharpe_delta = float(router_metrics.get("sharpe") or 0.0) - float(single_metrics.get("sharpe") or 0.0)
    expectancy_delta = float(router_metrics.get("expectancy") or 0.0) - float(single_metrics.get("expectancy") or 0.0)
    drawdown_delta = float(router_metrics.get("drawdown_pct") or 0.0) - float(single_metrics.get("drawdown_pct") or 0.0)
    return_delta = float(router_metrics.get("return_pct") or 0.0) - float(single_metrics.get("return_pct") or 0.0)

    robust_total = len(robustness_rows)
    robust_router_better = len([row for row in robustness_rows if bool(row.get("router_better"))])
    robust_ratio = robust_router_better / robust_total if robust_total else 0.0

    hypothesis = "A"
    status = "refutada"
    if pf_delta > 0 and sharpe_delta > 0 and return_delta > 0 and drawdown_delta <= 0 and robust_ratio >= 0.60:
        hypothesis = "B"
        status = "confirmada"
    elif (pf_delta > 0 or sharpe_delta > 0 or return_delta > 0) and robust_ratio >= 0.40:
        hypothesis = "B"
        status = "parcialmente_confirmada"

    return {
        "hypothesis_with_more_evidence": hypothesis,
        "conclusion": status,
        "router_vs_single": {
            "profit_factor_diff": round(pf_delta, 6),
            "sharpe_diff": round(sharpe_delta, 6),
            "expectancy_diff": round(expectancy_delta, 6),
            "drawdown_diff": round(drawdown_delta, 6),
            "return_diff": round(return_delta, 6),
            "robustness_diff": round(robust_ratio - 0.5, 6),
        },
        "router_superiority_contexts": {
            "router_better": robust_router_better,
            "total": robust_total,
            "ratio": round(robust_ratio, 6),
        },
    }


class MarketRegimeRouter:
    def __init__(self, mapping: list[dict[str, Any]], default_strategy: str, default_platform_strategy: str) -> None:
        self._by_context: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._global: dict[tuple[str, str], dict[str, Any]] = {}
        self._default_strategy = default_strategy
        self._default_platform_strategy = default_platform_strategy

        for row in mapping:
            key = (
                str(row.get("symbol") or "*"),
                str(row.get("timeframe") or "*"),
                str(row.get("trend_bucket") or "sideways"),
                str(row.get("vol_regime") or "normal_volatility"),
            )
            self._by_context[key] = row
            gk = (key[2], key[3])
            prev = self._global.get(gk)
            if prev is None or float(row.get("score") or 0.0) > float(prev.get("score") or 0.0):
                self._global[gk] = row

    def recommend(self, symbol: str, timeframe: str, trend_bucket: str, vol_regime: str) -> dict[str, Any]:
        key = (symbol, timeframe, trend_bucket, vol_regime)
        row = self._by_context.get(key)
        if row:
            return {
                "strategy": str(row.get("recommended_strategy") or self._default_strategy),
                "platform_strategy": str(row.get("recommended_platform_strategy") or self._default_platform_strategy),
                "source": "exact",
            }

        gk = (trend_bucket, vol_regime)
        fallback = self._global.get(gk)
        if fallback:
            return {
                "strategy": str(fallback.get("recommended_strategy") or self._default_strategy),
                "platform_strategy": str(fallback.get("recommended_platform_strategy") or self._default_platform_strategy),
                "source": "global_regime_fallback",
            }

        return {
            "strategy": self._default_strategy,
            "platform_strategy": self._default_platform_strategy,
            "source": "default",
        }


class MarketRegimeRouterService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        PHASE18_DIR.mkdir(parents=True, exist_ok=True)
        REGIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: MarketRegimeRouterConfig) -> dict[str, Any]:
        campaigns = robustness_analytics._load_phase13_campaigns()
        if not campaigns:
            raise RuntimeError("Nenhuma campanha da FASE 13 encontrada para FASE 18.")

        reference_report = self._load_reference_report(cfg, campaigns)
        candidates = self._select_candidates(reference_report, cfg)
        if not candidates:
            raise RuntimeError("Nenhuma estrategia candidata encontrada para Market Regime Router.")

        training_rows: list[dict[str, Any]] = []
        profile_rows: list[dict[str, Any]] = []

        for candidate in candidates:
            analyzed = self._analyze_candidate(candidate, cfg)
            training_rows.extend(analyzed["regime_rows"])
            profile_rows.append(analyzed["profile"])

        if not training_rows:
            raise RuntimeError("Nao foi possivel gerar linhas de regime para construir o router.")

        mapping = build_router_map(training_rows)
        baseline = self._select_baseline_strategy(profile_rows, cfg)
        router = MarketRegimeRouter(
            mapping=mapping,
            default_strategy=str(baseline.get("strategy") or ""),
            default_platform_strategy=str(baseline.get("platform_strategy_name") or ""),
        )

        simulation = self._run_simulation(cfg, candidates, router, baseline)
        hypothesis = evaluate_hypothesis(
            single_metrics=simulation["single_strategy_aggregate"],
            router_metrics=simulation["router_aggregate"],
            robustness_rows=simulation["robustness_rows"],
        )

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase": "FASE 18 - MARKET REGIME ROUTER",
            "source_campaign": {
                "run_id": reference_report.get("run_id"),
                "generated_at": reference_report.get("generated_at"),
                "symbol": reference_report.get("symbol"),
                "timeframe": reference_report.get("timeframe"),
            },
            "baseline_single_strategy": baseline,
            "strategy_profiles": profile_rows,
            "router_map": mapping,
            "simulation": simulation,
            "hypothesis_decision": hypothesis,
        }

        outputs = self._write_outputs(cfg.output_prefix, report, profile_rows, training_rows, mapping, simulation)
        return {
            "summary": {
                "hypothesis_with_more_evidence": hypothesis.get("hypothesis_with_more_evidence"),
                "conclusion": hypothesis.get("conclusion"),
                "baseline_single_strategy": baseline.get("strategy"),
                "router_superiority_ratio": hypothesis.get("router_superiority_contexts", {}).get("ratio"),
            },
            "report": report,
            "outputs": outputs,
        }

    def _load_reference_report(self, cfg: MarketRegimeRouterConfig, campaigns: list[robustness_analytics.Campaign]) -> dict[str, Any]:
        if cfg.report_file:
            payload = json.loads(Path(cfg.report_file).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Arquivo de campanha invalido para FASE 18.")
            return payload
        return campaigns[-1].payload

    def _select_candidates(self, report: dict[str, Any], cfg: MarketRegimeRouterConfig) -> list[dict[str, Any]]:
        backlog = report.get("backlog", []) if isinstance(report.get("backlog"), list) else []
        rows = [row for row in backlog if isinstance(row, dict)]

        if cfg.only_paper_candidates:
            rows = [row for row in rows if str(row.get("state") or "") == "PAPER_CANDIDATE"]

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
                    if cached.index.min() == candles.index.min() and cached.index.max() == candles.index.max():
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
                "return_pct": float(metrics.return_pct),
                "net_profit": float(metrics.net_profit),
                "win_rate": float(metrics.win_rate),
            },
        }

    def _analyze_candidate(self, candidate: dict[str, Any], cfg: MarketRegimeRouterConfig) -> dict[str, Any]:
        strategy = str(candidate.get("candidate_name") or "").strip()
        platform_strategy = str(candidate.get("platform_strategy_name") or strategy).strip()
        family = str(candidate.get("family") or "unknown")

        regime_rows: list[dict[str, Any]] = []
        timeframe_rows: list[dict[str, Any]] = []

        for symbol in cfg.symbols:
            for timeframe in cfg.timeframes:
                candles = self._load_market_data(symbol, timeframe, cfg.window_days, cfg.max_bars)
                if candles.empty:
                    continue

                regimes = self._load_or_build_regimes(symbol, timeframe, candles)
                backtest = self._run_backtest(platform_strategy, symbol, timeframe, candles, cfg.capital)
                trades = backtest["trades"]
                trade_regimes = attach_trade_regimes(trades, regimes) if not trades.empty else trades.copy()

                full_row = {
                    "strategy": strategy,
                    "platform_strategy_name": platform_strategy,
                    "family": family,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    **backtest["metrics"],
                }
                full_row["context_score"] = _context_score(full_row)
                timeframe_rows.append(full_row)

                for trend_bucket in TREND_REGIMES:
                    for vol_regime in VOL_REGIMES:
                        if trade_regimes.empty:
                            subset = trade_regimes
                        else:
                            subset = trade_regimes[
                                (trade_regimes["trend_bucket"] == trend_bucket)
                                & (trade_regimes["vol_regime"] == vol_regime)
                            ]
                        metrics = metrics_from_trades(subset, cfg.capital)
                        row = {
                            "strategy": strategy,
                            "platform_strategy_name": platform_strategy,
                            "family": family,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "trend_bucket": trend_bucket,
                            "vol_regime": vol_regime,
                            "regime": f"{trend_bucket}|{vol_regime}",
                            **metrics,
                        }
                        row["context_score"] = _context_score(row)
                        regime_rows.append(row)

        valid_regimes = [row for row in regime_rows if int(row.get("number_of_trades") or 0) >= cfg.min_trades_per_regime]
        best_regime = max(valid_regimes, key=lambda x: float(x.get("context_score") or 0.0), default=None)
        worst_regime = min(valid_regimes, key=lambda x: float(x.get("context_score") or 0.0), default=None)

        best_timeframe = None
        best_asset = None
        if timeframe_rows:
            by_tf: dict[str, list[float]] = {}
            by_asset: dict[str, list[float]] = {}
            for row in timeframe_rows:
                by_tf.setdefault(str(row.get("timeframe")), []).append(float(row.get("context_score") or 0.0))
                by_asset.setdefault(str(row.get("symbol")), []).append(float(row.get("context_score") or 0.0))
            best_timeframe = max(by_tf, key=lambda k: _avg(by_tf[k]) or 0.0) if by_tf else None
            best_asset = max(by_asset, key=lambda k: _avg(by_asset[k]) or 0.0) if by_asset else None

        regime_metrics: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in valid_regimes:
            grouped.setdefault(str(row.get("regime")), []).append(row)
        for regime, rows in grouped.items():
            regime_metrics.append(
                {
                    "regime": regime,
                    "number_of_trades": int(sum(float(r.get("number_of_trades") or 0.0) for r in rows)),
                    "profit_factor": _avg([float(r.get("profit_factor") or 0.0) for r in rows]),
                    "sharpe": _avg([float(r.get("sharpe") or 0.0) for r in rows]),
                    "expectancy": _avg([float(r.get("expectancy") or 0.0) for r in rows]),
                    "drawdown_pct": _avg([float(r.get("drawdown_pct") or 0.0) for r in rows]),
                }
            )

        profile = {
            "strategy": strategy,
            "platform_strategy_name": platform_strategy,
            "family": family,
            "best_regime": best_regime.get("regime") if best_regime else None,
            "worst_regime": worst_regime.get("regime") if worst_regime else None,
            "best_timeframe": best_timeframe,
            "best_asset": best_asset,
            "regime_metrics": sorted(regime_metrics, key=lambda x: str(x.get("regime"))),
            "operating_conditions": self._operating_conditions(best_regime, best_timeframe, best_asset),
            "global_score": _avg([float(r.get("context_score") or 0.0) for r in valid_regimes]) or 0.0,
        }

        return {
            "profile": profile,
            "regime_rows": valid_regimes,
        }

    def _operating_conditions(self, best_regime: dict[str, Any] | None, best_timeframe: str | None, best_asset: str | None) -> str:
        if not best_regime:
            return "operar apenas em modo observacao ate acumular amostra minima"
        trend = str(best_regime.get("trend_bucket") or "sideways")
        vol = str(best_regime.get("vol_regime") or "normal_volatility")
        parts = [f"trend={trend}", f"volatility={vol}"]
        if best_timeframe:
            parts.append(f"timeframe={best_timeframe}")
        if best_asset:
            parts.append(f"asset={best_asset}")
        return "operar preferencialmente quando " + ", ".join(parts)

    def _select_baseline_strategy(self, profile_rows: list[dict[str, Any]], cfg: MarketRegimeRouterConfig) -> dict[str, Any]:
        if cfg.baseline_strategy:
            by_name = {
                str(row.get("strategy")): row
                for row in profile_rows
            }
            by_platform = {
                str(row.get("platform_strategy_name")): row
                for row in profile_rows
            }
            explicit = by_name.get(cfg.baseline_strategy) or by_platform.get(cfg.baseline_strategy)
            if explicit:
                return {
                    "strategy": explicit.get("strategy"),
                    "platform_strategy_name": explicit.get("platform_strategy_name"),
                    "selection_method": "explicit_cli",
                    "score": explicit.get("global_score"),
                }

        best = max(profile_rows, key=lambda row: float(row.get("global_score") or 0.0), default=None)
        if best is None:
            raise RuntimeError("Nao foi possivel selecionar estrategia baseline para o cenario A.")

        return {
            "strategy": best.get("strategy"),
            "platform_strategy_name": best.get("platform_strategy_name"),
            "selection_method": "best_global_score",
            "score": best.get("global_score"),
        }

    def _run_simulation(
        self,
        cfg: MarketRegimeRouterConfig,
        candidates: list[dict[str, Any]],
        router: MarketRegimeRouter,
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        strategy_name_map: dict[str, str] = {}
        for item in candidates:
            strategy_name = str(item.get("candidate_name") or "").strip()
            platform_name = str(item.get("platform_strategy_name") or strategy_name).strip()
            if strategy_name and platform_name:
                strategy_name_map[strategy_name] = platform_name

        baseline_platform = str(baseline.get("platform_strategy_name") or "")
        candidate_platforms = sorted(set(strategy_name_map.values()))

        single_context_rows: list[dict[str, Any]] = []
        router_context_rows: list[dict[str, Any]] = []

        agg_single_trades: list[dict[str, Any]] = []
        agg_router_trades: list[dict[str, Any]] = []

        for symbol in cfg.symbols:
            for timeframe in cfg.timeframes:
                candles = self._load_market_data(symbol, timeframe, cfg.window_days, cfg.max_bars)
                if candles.empty:
                    continue

                regimes = self._load_or_build_regimes(symbol, timeframe, candles)

                single_bt = self._run_backtest(baseline_platform, symbol, timeframe, candles, cfg.capital)
                single_metrics = single_bt["metrics"]
                single_context_rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "scenario": "single_strategy",
                        "strategy": baseline.get("strategy"),
                        "platform_strategy_name": baseline_platform,
                        **single_metrics,
                    }
                )

                single_trades = single_bt["trades"].copy()
                if not single_trades.empty:
                    single_trades["symbol"] = symbol
                    single_trades["timeframe"] = timeframe
                    agg_single_trades.extend(single_trades.to_dict("records"))

                strategy_trade_books: dict[str, pd.DataFrame] = {}
                for platform_name in candidate_platforms:
                    bt = self._run_backtest(platform_name, symbol, timeframe, candles, cfg.capital)
                    trades_df = bt["trades"]
                    if trades_df.empty:
                        strategy_trade_books[platform_name] = trades_df
                        continue
                    trades_df = attach_trade_regimes(trades_df, regimes)
                    trades_df["platform_strategy_name"] = platform_name
                    strategy_trade_books[platform_name] = trades_df

                routed = self._compose_router_trades(symbol, timeframe, strategy_trade_books, router)
                router_metrics = metrics_from_trades(routed, cfg.capital)
                router_context_rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "scenario": "market_regime_router",
                        "strategy": "dynamic",
                        "platform_strategy_name": "dynamic",
                        **router_metrics,
                    }
                )

                if not routed.empty:
                    routed["symbol"] = symbol
                    routed["timeframe"] = timeframe
                    agg_router_trades.extend(routed.to_dict("records"))

        single_aggregate = metrics_from_trades(pd.DataFrame(agg_single_trades), cfg.capital)
        router_aggregate = metrics_from_trades(pd.DataFrame(agg_router_trades), cfg.capital)

        robustness_rows: list[dict[str, Any]] = []
        by_context = {
            (str(r.get("symbol")), str(r.get("timeframe"))): r
            for r in router_context_rows
        }
        for single in single_context_rows:
            key = (str(single.get("symbol")), str(single.get("timeframe")))
            routed = by_context.get(key, {})
            row = {
                "symbol": key[0],
                "timeframe": key[1],
                "single_profit_factor": _safe_float(single.get("profit_factor")),
                "router_profit_factor": _safe_float(routed.get("profit_factor")),
                "single_sharpe": _safe_float(single.get("sharpe")),
                "router_sharpe": _safe_float(routed.get("sharpe")),
                "single_expectancy": _safe_float(single.get("expectancy")),
                "router_expectancy": _safe_float(routed.get("expectancy")),
                "single_drawdown_pct": _safe_float(single.get("drawdown_pct")),
                "router_drawdown_pct": _safe_float(routed.get("drawdown_pct")),
                "single_return_pct": _safe_float(single.get("return_pct")),
                "router_return_pct": _safe_float(routed.get("return_pct")),
            }
            row["router_better"] = self._is_router_better(row)
            robustness_rows.append(row)

        return {
            "single_strategy_context_rows": single_context_rows,
            "router_context_rows": router_context_rows,
            "single_strategy_aggregate": single_aggregate,
            "router_aggregate": router_aggregate,
            "comparison": {
                "profit_factor_diff": round(float(router_aggregate.get("profit_factor") or 0.0) - float(single_aggregate.get("profit_factor") or 0.0), 6),
                "sharpe_diff": round(float(router_aggregate.get("sharpe") or 0.0) - float(single_aggregate.get("sharpe") or 0.0), 6),
                "expectancy_diff": round(float(router_aggregate.get("expectancy") or 0.0) - float(single_aggregate.get("expectancy") or 0.0), 6),
                "drawdown_diff": round(float(router_aggregate.get("drawdown_pct") or 0.0) - float(single_aggregate.get("drawdown_pct") or 0.0), 6),
                "return_diff": round(float(router_aggregate.get("return_pct") or 0.0) - float(single_aggregate.get("return_pct") or 0.0), 6),
                "win_rate_diff": round(float(router_aggregate.get("win_rate") or 0.0) - float(single_aggregate.get("win_rate") or 0.0), 6),
                "trades_diff": int(float(router_aggregate.get("number_of_trades") or 0.0) - float(single_aggregate.get("number_of_trades") or 0.0)),
            },
            "robustness_rows": robustness_rows,
        }

    def _is_router_better(self, row: dict[str, Any]) -> bool:
        pf_gain = float(row.get("router_profit_factor") or 0.0) - float(row.get("single_profit_factor") or 0.0)
        sharpe_gain = float(row.get("router_sharpe") or 0.0) - float(row.get("single_sharpe") or 0.0)
        ret_gain = float(row.get("router_return_pct") or 0.0) - float(row.get("single_return_pct") or 0.0)
        dd_gain = float(row.get("router_drawdown_pct") or 0.0) - float(row.get("single_drawdown_pct") or 0.0)
        score = 0
        if pf_gain > 0:
            score += 1
        if sharpe_gain > 0:
            score += 1
        if ret_gain > 0:
            score += 1
        if dd_gain <= 0:
            score += 1
        return score >= 3

    def _compose_router_trades(
        self,
        symbol: str,
        timeframe: str,
        strategy_trade_books: dict[str, pd.DataFrame],
        router: MarketRegimeRouter,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for platform_name, trades in strategy_trade_books.items():
            if trades.empty:
                continue
            trades = trades.copy()
            trades["platform_strategy_name"] = platform_name
            rows.extend(trades.to_dict("records"))

        if not rows:
            return pd.DataFrame()

        merged = pd.DataFrame(rows)
        merged["entry_time"] = pd.to_datetime(merged["entry_time"], utc=True)
        merged["exit_time"] = pd.to_datetime(merged["exit_time"], utc=True)

        selected: list[dict[str, Any]] = []
        for row in merged.sort_values("entry_time").to_dict("records"):
            trend_bucket = str(row.get("trend_bucket") or "sideways")
            vol_regime = str(row.get("vol_regime") or "normal_volatility")
            recommendation = router.recommend(symbol, timeframe, trend_bucket, vol_regime)
            if str(row.get("platform_strategy_name") or "") != str(recommendation.get("platform_strategy") or ""):
                continue
            row["router_selected_strategy"] = recommendation.get("strategy")
            row["router_selection_source"] = recommendation.get("source")
            selected.append(row)

        if not selected:
            return pd.DataFrame()

        # Keep single active position semantics in router simulation.
        accepted: list[dict[str, Any]] = []
        active_until = None
        for row in sorted(selected, key=lambda x: pd.Timestamp(x["entry_time"])):
            entry_time = pd.Timestamp(row["entry_time"])
            exit_time = pd.Timestamp(row["exit_time"])
            if active_until is not None and entry_time < active_until:
                continue
            accepted.append(row)
            active_until = exit_time

        return pd.DataFrame(accepted)

    def _write_outputs(
        self,
        output_prefix: str,
        report: dict[str, Any],
        profiles: list[dict[str, Any]],
        training_rows: list[dict[str, Any]],
        mapping: list[dict[str, Any]],
        simulation: dict[str, Any],
    ) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = PHASE18_DIR / f"{output_prefix}_{stamp}.json"
        md_path = PHASE18_DIR / f"{output_prefix}_{stamp}.md"
        profiles_csv = PHASE18_DIR / f"{output_prefix}_{stamp}_profiles.csv"
        regime_training_csv = PHASE18_DIR / f"{output_prefix}_{stamp}_regime_training.csv"
        router_map_csv = PHASE18_DIR / f"{output_prefix}_{stamp}_router_map.csv"
        robustness_csv = PHASE18_DIR / f"{output_prefix}_{stamp}_robustness.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._render_markdown(report), encoding="utf-8")

        self._write_csv(profiles_csv, profiles)
        self._write_csv(regime_training_csv, training_rows)
        self._write_csv(router_map_csv, mapping)
        self._write_csv(robustness_csv, simulation.get("robustness_rows", []))

        return {
            "json": str(json_path),
            "md": str(md_path),
            "profiles_csv": str(profiles_csv),
            "regime_training_csv": str(regime_training_csv),
            "router_map_csv": str(router_map_csv),
            "robustness_csv": str(robustness_csv),
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
        baseline = report.get("baseline_single_strategy", {}) if isinstance(report.get("baseline_single_strategy"), dict) else {}
        simulation = report.get("simulation", {}) if isinstance(report.get("simulation"), dict) else {}
        comparison = simulation.get("comparison", {}) if isinstance(simulation.get("comparison"), dict) else {}
        hypothesis = report.get("hypothesis_decision", {}) if isinstance(report.get("hypothesis_decision"), dict) else {}

        lines = [
            "# FASE 18 - MARKET REGIME ROUTER",
            "",
            "## Estrategia Unica (Cenario A)",
            f"- Estrategia baseline: {baseline.get('strategy')} ({baseline.get('platform_strategy_name')})",
            f"- Metodo de selecao: {baseline.get('selection_method')}",
            f"- Score base: {baseline.get('score')}",
            "",
            "## Router (Cenario B)",
            "- Selecao dinamica por regime de tendencia + volatilidade.",
            "",
            "## Comparacao",
            f"- Diferenca Profit Factor: {comparison.get('profit_factor_diff')}",
            f"- Diferenca Sharpe: {comparison.get('sharpe_diff')}",
            f"- Diferenca Drawdown: {comparison.get('drawdown_diff')}",
            f"- Diferenca Retorno: {comparison.get('return_diff')}",
            f"- Diferenca Expectancy: {comparison.get('expectancy_diff')}",
            f"- Diferenca Win Rate: {comparison.get('win_rate_diff')}",
            f"- Diferenca Numero de Operacoes: {comparison.get('trades_diff')}",
            "",
            "## Robustez",
        ]

        robustness_rows = simulation.get("robustness_rows", []) if isinstance(simulation.get("robustness_rows"), list) else []
        if robustness_rows:
            for row in robustness_rows:
                lines.append(
                    "- "
                    f"{row.get('symbol')}/{row.get('timeframe')} | "
                    f"router_better={'SIM' if row.get('router_better') else 'NAO'} | "
                    f"PF(single={row.get('single_profit_factor')}, router={row.get('router_profit_factor')}) | "
                    f"Sharpe(single={row.get('single_sharpe')}, router={row.get('router_sharpe')})"
                )
        else:
            lines.append("- Sem dados de robustez para os contextos selecionados.")

        lines.extend(
            [
                "",
                "## Decisao",
                f"- Hipotese com maior evidencia: {hypothesis.get('hypothesis_with_more_evidence')}",
                f"- Conclusao: {hypothesis.get('conclusion')}",
                f"- Router superiority ratio: {hypothesis.get('router_superiority_contexts', {}).get('ratio')}",
                "",
                "## Criterio Cientifico",
                "- Nenhuma estrategia foi modificada.",
                "- RR, Optimizer, Validation e criterios cientificos foram preservados.",
                "- Variavel nova isolada: selecao dinamica da estrategia por regime.",
                "",
            ]
        )

        return "\n".join(lines)
