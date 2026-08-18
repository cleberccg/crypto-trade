from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backtesting.engine import BacktestConfig, BacktestEngine
from database.history_models import OptimizationResultRecord, TradeHistory
from database.models import Candle
from database.repositories import CandleRepository
from utils.metrics import expectancy_from_pnl, max_drawdown_from_pnl, profit_factor_from_pnl, sharpe_from_pnl, win_rate_from_pnl
from strategies.factory import create_strategy
from utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class ResearchLabConfig:
    strategies: list[str]
    symbol: str | None = None
    timeframe: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    horizon_bars: int = 12
    max_candidates_per_strategy: int = 20


@contextmanager
def _quiet_replay_logs() -> Any:
    names = [
        "strategies.trend_v1",
        "strategies.trend_v2",
        "backtesting.engine",
        "backtesting.metrics",
        "risk.risk_manager",
        "risk.position_sizer",
    ]
    old_levels: dict[str, int] = {}
    for name in names:
        logger = logging.getLogger(name)
        old_levels[name] = logger.level
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for name in names:
            logging.getLogger(name).setLevel(old_levels[name])


class StrategyResearchLab:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, config: ResearchLabConfig) -> dict[str, Any]:
        t0 = datetime.now(timezone.utc)
        logger.info("StrategyResearchLab — iniciado | strategies=%s symbol=%s timeframe=%s", config.strategies, config.symbol, config.timeframe)
        trades = self._load_trade_history(config)
        logger.info("StrategyResearchLab — operacoes carregadas: %d", len(trades))
        source = "trade_history"
        reconstruction_meta: dict[str, Any] = {}

        if trades.empty:
            trades, reconstruction_meta = self._reconstruct_from_optimization(config)
            source = "reconstructed_from_optimization"
            logger.info("StrategyResearchLab — reconstruidas operacoes: %d", len(trades))

        if trades.empty:
            raise ValueError("Sem operacoes para pesquisa: trade_history vazio e reconstrucao falhou.")

        consolidated = self._normalize_operations(trades)
        if consolidated.empty:
            raise ValueError("Consolidacao gerou base vazia apos deduplicacao.")

        symbol_mode = config.symbol or str(consolidated["symbol"].mode().iloc[0])
        timeframe_mode = config.timeframe or str(consolidated["timeframe"].mode().iloc[0])

        candles = self._load_candles(symbol=symbol_mode, timeframe=timeframe_mode, trades=consolidated)
        if candles.empty:
            raise ValueError("Nao foi possivel carregar candles para classificar regimes.")
        logger.info("StrategyResearchLab — candles carregados: %d", len(candles))

        candles_regimes, criteria = self._build_market_regimes(candles)
        enriched = self._attach_regimes_and_profiles(consolidated, candles_regimes, config.horizon_bars)

        stage1 = self._stage1_autopsy(enriched)
        stage2 = self._stage2_profile_distribution(enriched)
        stage3 = self._stage3_regime_statistics(enriched)
        logger.info("StrategyResearchLab — stage3 concluido | regimes=%d", len(stage3) if hasattr(stage3, '__len__') else 0)
        stage5 = self._stage5_hypothesis_tests(enriched, stage3)
        logger.info("StrategyResearchLab — stage5 concluido | hypotheses=%d", len(stage5) if hasattr(stage5, '__len__') else 0)
        stage6 = self._stage6_market_regime_detector_design(stage5)
        stage7 = self._stage7_trendv3_gate(stage5)

        final_report = self._build_final_report(
            source=source,
            reconstruction_meta=reconstruction_meta,
            consolidated=enriched,
            stage3=stage3,
            stage5=stage5,
            stage6=stage6,
            stage7=stage7,
        )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "strategies": config.strategies,
                "symbol": symbol_mode,
                "timeframe": timeframe_mode,
                "start": config.start.isoformat() if config.start else None,
                "end": config.end.isoformat() if config.end else None,
                "horizon_bars": config.horizon_bars,
                "max_candidates_per_strategy": config.max_candidates_per_strategy,
            },
            "trade_source": source,
            "reconstruction_meta": reconstruction_meta,
            "regime_criteria": criteria,
            "stage1_autopsy": stage1,
            "stage2_trade_profile": stage2,
            "stage3_market_regimes": stage3,
            "stage5_hypothesis_tests": stage5,
            "stage6_market_regime_detector": stage6,
            "stage7_trendv3_gate": stage7,
            "final_report": final_report,
        }

        outputs = self._write_outputs(payload, enriched, candles_regimes)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        logger.info("StrategyResearchLab — concluido em %.2fs | operations=%d", elapsed, len(enriched))

        return {
            "summary": {
                "total_operations": int(len(enriched)),
                "trade_source": source,
                "reconstruction_meta": reconstruction_meta,
                "h1_status": self._find_hypothesis_status(stage5, "H1"),
                "recommendation": final_report["recommendation"],
            },
            "outputs": outputs,
        }

    def _load_trade_history(self, config: ResearchLabConfig) -> pd.DataFrame:
        stmt = select(TradeHistory).where(TradeHistory.exit_time.is_not(None))
        if config.strategies:
            stmt = stmt.where(TradeHistory.strategy.in_(config.strategies))
        if config.symbol:
            stmt = stmt.where(TradeHistory.symbol == config.symbol)
        if config.timeframe:
            stmt = stmt.where(TradeHistory.timeframe == config.timeframe)
        if config.start:
            stmt = stmt.where(TradeHistory.entry_time >= config.start)
        if config.end:
            stmt = stmt.where(TradeHistory.exit_time <= config.end)

        rows = self._session.execute(stmt).scalars().all()
        data = []
        for row in rows:
            data.append(
                {
                    "operation_id": f"db_{row.id}",
                    "execution_id": row.execution_id,
                    "strategy": row.strategy,
                    "symbol": row.symbol,
                    "timeframe": row.timeframe,
                    "parameters_json": None,
                    "entry_time": row.entry_time,
                    "exit_time": row.exit_time,
                    "entry_price": row.entry_price,
                    "exit_price": row.exit_price,
                    "stop_loss": row.stop_loss,
                    "take_profit": row.take_profit,
                    "risk_reward": row.risk_reward,
                    "quantity": row.quantity,
                    "pnl": row.pnl,
                    "pnl_percent": row.pnl_percent,
                    "duration_minutes": row.duration_minutes,
                    "exit_reason": row.exit_reason,
                }
            )

        df = pd.DataFrame(data)
        if df.empty:
            return df
        return self._normalize_operations(df)

    def _reconstruct_from_optimization(self, config: ResearchLabConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
        rows_by_strategy: dict[str, list[OptimizationResultRecord]] = {}
        meta: dict[str, Any] = {}

        for strategy_name in config.strategies:
            stmt = select(OptimizationResultRecord).where(OptimizationResultRecord.strategy == strategy_name)
            if config.symbol:
                stmt = stmt.where(OptimizationResultRecord.symbol == config.symbol)
            if config.timeframe:
                stmt = stmt.where(OptimizationResultRecord.timeframe == config.timeframe)
            all_rows = self._session.execute(stmt).scalars().all()

            if not all_rows:
                rows_by_strategy[strategy_name] = []
                meta[strategy_name] = {
                    "available": 0,
                    "selected": 0,
                    "policy": "no_rows",
                    "requested_top": 0,
                }
                continue

            ranked = self._rank_optimization_candidates(all_rows)
            available = len(ranked)
            requested_top = 20 if available >= 20 else min(10, available)
            requested_top = min(requested_top, max(1, int(config.max_candidates_per_strategy)))
            selected = ranked[:requested_top]
            rows_by_strategy[strategy_name] = selected
            meta[strategy_name] = {
                "available": available,
                "requested_top": requested_top,
                "selected": len(selected),
                "policy": "top20_if_available_else_top10",
            }

        candles_cache: dict[tuple[str, str], pd.DataFrame] = {}
        payload: list[dict[str, Any]] = []

        with _quiet_replay_logs():
            for strategy_name, rows in rows_by_strategy.items():
                successful = 0
                failed = 0
                for row in rows:
                    symbol = row.symbol
                    timeframe = row.timeframe
                    key = (symbol, timeframe)
                    if key not in candles_cache:
                        candles_cache[key] = self._load_backtest_dataframe(symbol, timeframe, config.start, config.end)
                    df_bars = candles_cache[key]
                    if df_bars.empty:
                        failed += 1
                        continue

                    parameters = json.loads(row.parameters_json) if row.parameters_json else {}
                    strategy = create_strategy(strategy_name, **parameters)
                    strategy.initialize()
                    strategy.prepare_dataset(df_bars.copy(), symbol=symbol, timeframe=timeframe)
                    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=10_000.0))
                    try:
                        result = engine.run(df_bars.copy(), symbol=symbol, timeframe=timeframe)
                    except Exception:
                        failed += 1
                        continue

                    successful += 1
                    for trade in result.trades:
                        payload.append(
                            {
                                "operation_id": f"replay_{strategy_name}_{row.id}_{trade.get('entry_bar', 0)}_{trade.get('exit_bar', 0)}",
                                "execution_id": row.execution_id,
                                "strategy": strategy_name,
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "parameters_json": row.parameters_json,
                                "entry_time": trade.get("entry_time"),
                                "exit_time": trade.get("exit_time"),
                                "entry_price": trade.get("entry_price"),
                                "exit_price": trade.get("exit_price"),
                                "stop_loss": trade.get("stop_loss"),
                                "take_profit": trade.get("take_profit"),
                                "risk_reward": parameters.get("risk_reward_ratio"),
                                "quantity": trade.get("quantity"),
                                "pnl": trade.get("pnl"),
                                "pnl_percent": trade.get("pnl_pct"),
                                "duration_minutes": trade.get("duration_minutes"),
                                "exit_reason": trade.get("exit_reason"),
                            }
                        )

                meta[strategy_name]["replay_successful"] = successful
                meta[strategy_name]["replay_failed"] = failed

        df = pd.DataFrame(payload)
        if df.empty:
            return df, meta

        return self._normalize_operations(df), meta

    def _rank_optimization_candidates(self, rows: list[OptimizationResultRecord]) -> list[OptimizationResultRecord]:
        if not rows:
            return []

        frame = pd.DataFrame(
            [
                {
                    "row": item,
                    "profit_factor": float(item.profit_factor) if item.profit_factor is not None else np.nan,
                    "net_profit": float(item.net_profit) if item.net_profit is not None else np.nan,
                    "sharpe": float(item.sharpe) if item.sharpe is not None else np.nan,
                    "trades": float(item.trades) if item.trades is not None else np.nan,
                    "drawdown": float(item.drawdown) if item.drawdown is not None else np.nan,
                }
                for item in rows
            ]
        )

        frame["profit_factor"] = frame["profit_factor"].replace([np.inf, -np.inf], np.nan)
        frame["sharpe"] = frame["sharpe"].replace([np.inf, -np.inf], np.nan)
        frame["score"] = (
            0.35 * frame["profit_factor"].fillna(0).rank(pct=True)
            + 0.25 * frame["net_profit"].fillna(frame["net_profit"].median(skipna=True)).rank(pct=True)
            + 0.20 * frame["sharpe"].fillna(frame["sharpe"].median(skipna=True)).rank(pct=True)
            + 0.10 * frame["trades"].fillna(0).rank(pct=True)
            + 0.10 * (1 - frame["drawdown"].fillna(frame["drawdown"].median(skipna=True)).rank(pct=True))
        )
        frame = frame.sort_values(["score", "trades", "net_profit"], ascending=[False, False, False])
        return frame["row"].tolist()

    def _normalize_operations(self, df: pd.DataFrame) -> pd.DataFrame:
        work = df.copy()
        work["entry_time"] = pd.to_datetime(work["entry_time"], utc=True)
        work["exit_time"] = pd.to_datetime(work["exit_time"], utc=True)

        numeric = [
            "entry_price",
            "exit_price",
            "stop_loss",
            "take_profit",
            "risk_reward",
            "quantity",
            "pnl",
            "pnl_percent",
            "duration_minutes",
        ]
        for col in numeric:
            work[col] = pd.to_numeric(work[col], errors="coerce")

        missing_duration = work["duration_minutes"].isna()
        work.loc[missing_duration, "duration_minutes"] = (
            (work.loc[missing_duration, "exit_time"] - work.loc[missing_duration, "entry_time"]).dt.total_seconds()
            / 60.0
        )

        work["duration_minutes"] = work["duration_minutes"].fillna(0.0).clip(lower=0.0)
        work["exit_reason"] = work["exit_reason"].fillna("unknown")

        dedup_cols = [
            "strategy",
            "symbol",
            "timeframe",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "pnl",
        ]
        work = work.drop_duplicates(subset=dedup_cols).sort_values("entry_time").reset_index(drop=True)
        return work

    def _load_backtest_dataframe(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None,
        end: datetime | None,
    ) -> pd.DataFrame:
        repo = CandleRepository(self._session)
        final_end = end or datetime.now(tz=timezone.utc)
        final_start = start or (final_end - timedelta(days=120))

        rows = repo.get_range(symbol=symbol, timeframe=timeframe, start=final_start, end=final_end)
        if not rows and "/" in symbol:
            rows = repo.get_range(
                symbol=symbol.replace("/", ""),
                timeframe=timeframe,
                start=final_start,
                end=final_end,
            )
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(
            [
                {
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "close": item.close,
                    "volume": item.volume,
                }
                for item in rows
            ],
            index=pd.DatetimeIndex([item.open_time for item in rows], tz="UTC"),
        )
        return frame.sort_index()

    def _load_candles(self, symbol: str, timeframe: str, trades: pd.DataFrame) -> pd.DataFrame:
        start = pd.to_datetime(trades["entry_time"].min(), utc=True).to_pydatetime() - timedelta(days=5)
        end = pd.to_datetime(trades["exit_time"].max(), utc=True).to_pydatetime() + timedelta(days=2)

        symbols = [symbol]
        if "/" in symbol:
            symbols.append(symbol.replace("/", ""))

        stmt = (
            select(Candle)
            .where(Candle.symbol.in_(symbols))
            .where(Candle.timeframe == timeframe)
            .where(Candle.open_time >= start)
            .where(Candle.open_time <= end)
            .order_by(Candle.open_time.asc())
        )
        rows = self._session.execute(stmt).scalars().all()

        data = [
            {
                "timestamp": row.open_time,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for row in rows
        ]
        candles = pd.DataFrame(data)
        if candles.empty:
            return candles

        candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            candles[col] = pd.to_numeric(candles[col], errors="coerce")
        return candles.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)

    def _build_market_regimes(self, candles: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        df = candles.copy()
        df["ema_fast"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=50, adjust=False).mean()
        df["trend_score"] = (df["ema_fast"] - df["ema_slow"]) / df["close"].replace(0, np.nan)

        abs_trend = df["trend_score"].abs().replace([np.inf, -np.inf], np.nan)
        strong_thr = float(abs_trend.quantile(0.80)) if abs_trend.notna().any() else 0.0018
        moderate_thr = float(abs_trend.quantile(0.55)) if abs_trend.notna().any() else 0.0009
        strong_thr = max(strong_thr, 0.0012)
        moderate_thr = max(moderate_thr, 0.0006)

        df["trend_regime"] = "consolidacao"
        df.loc[df["trend_score"] >= moderate_thr, "trend_regime"] = "tendencia_moderada_alta"
        df.loc[df["trend_score"] >= strong_thr, "trend_regime"] = "tendencia_forte_alta"
        df.loc[df["trend_score"] <= -moderate_thr, "trend_regime"] = "tendencia_moderada_baixa"
        df.loc[df["trend_score"] <= -strong_thr, "trend_regime"] = "tendencia_forte_baixa"

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr14"] = tr.rolling(14, min_periods=8).mean()
        df["atr_pct"] = df["atr14"] / df["close"].replace(0, np.nan)

        low_vol_thr = float(df["atr_pct"].quantile(0.30)) if df["atr_pct"].notna().any() else 0.0
        high_vol_thr = float(df["atr_pct"].quantile(0.70)) if df["atr_pct"].notna().any() else 0.0
        df["vol_regime"] = "volatilidade_media"
        df.loc[df["atr_pct"] <= low_vol_thr, "vol_regime"] = "baixa_volatilidade"
        df.loc[df["atr_pct"] >= high_vol_thr, "vol_regime"] = "alta_volatilidade"

        rolling_high = df["high"].rolling(20, min_periods=10).max().shift(1)
        rolling_low = df["low"].rolling(20, min_periods=10).min().shift(1)
        breakout_eps = 0.0005
        df["breakout_up"] = df["close"] > (rolling_high * (1 + breakout_eps))
        df["breakout_down"] = df["close"] < (rolling_low * (1 - breakout_eps))
        df["is_breakout"] = df["breakout_up"] | df["breakout_down"]

        criteria = {
            "trend_moderate_threshold": moderate_thr,
            "trend_strong_threshold": strong_thr,
            "low_vol_threshold": low_vol_thr,
            "high_vol_threshold": high_vol_thr,
            "breakout_epsilon": breakout_eps,
            "atr_window": 14,
            "trend_ema_fast": 20,
            "trend_ema_slow": 50,
            "breakout_window": 20,
        }
        return df, criteria

    def _attach_regimes_and_profiles(self, trades: pd.DataFrame, candles: pd.DataFrame, horizon_bars: int) -> pd.DataFrame:
        left = trades.copy().sort_values("entry_time")
        right = candles.copy().sort_values("timestamp")

        left["entry_time"] = pd.to_datetime(left["entry_time"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
        left["exit_time"] = pd.to_datetime(left["exit_time"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
        right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)

        cols = [
            "timestamp",
            "close",
            "trend_regime",
            "vol_regime",
            "trend_score",
            "atr_pct",
            "is_breakout",
            "breakout_up",
            "breakout_down",
        ]

        merged = pd.merge_asof(
            left,
            right[cols],
            left_on="entry_time",
            right_on="timestamp",
            direction="backward",
            tolerance=pd.Timedelta("5D"),
        )
        merged = merged.rename(
            columns={
                "close": "entry_candle_close",
                "trend_regime": "entry_trend_regime",
                "vol_regime": "entry_vol_regime",
                "trend_score": "entry_trend_score",
                "atr_pct": "entry_atr_pct",
                "is_breakout": "entry_is_breakout",
            }
        )
        merged = merged.drop(columns=["timestamp"])

        candle_times = right["timestamp"].to_numpy(dtype="datetime64[ns]")
        perf_rows: list[dict[str, Any]] = []
        for _, row in merged.iterrows():
            entry_time = pd.to_datetime(row["entry_time"])
            exit_time = pd.to_datetime(row["exit_time"])
            entry_price = float(row["entry_price"])
            exit_price = float(row["exit_price"]) if pd.notna(row["exit_price"]) else entry_price

            entry_idx = int(np.searchsorted(candle_times, np.datetime64(entry_time), side="left"))
            exit_idx = int(np.searchsorted(candle_times, np.datetime64(exit_time), side="right")) - 1
            entry_idx = min(max(entry_idx, 0), len(right) - 1)
            exit_idx = min(max(exit_idx, entry_idx), len(right) - 1)
            seg = right.iloc[entry_idx : exit_idx + 1]

            side_sign = 1.0
            if float(row.get("quantity", 1.0) or 1.0) < 0:
                side_sign = -1.0

            if seg.empty:
                first_move = 0.0
                final_move = 0.0
                mae = 0.0
                mfe = 0.0
            else:
                horizon_idx = min(entry_idx + max(1, int(horizon_bars)), len(right) - 1, exit_idx)
                horizon_price = float(right.iloc[horizon_idx]["close"])
                first_move = ((horizon_price - entry_price) / entry_price) * side_sign
                final_move = ((exit_price - entry_price) / entry_price) * side_sign
                mfe = max(((float(seg["high"].max()) - entry_price) / entry_price) * side_sign, 0.0)
                mae = min(((float(seg["low"].min()) - entry_price) / entry_price) * side_sign, 0.0)

            perf_rows.append(
                {
                    "first_move": first_move,
                    "final_move": final_move,
                    "mfe": mfe,
                    "mae": mae,
                }
            )

        perf = pd.DataFrame(perf_rows)
        enriched = pd.concat([merged.reset_index(drop=True), perf], axis=1)

        reason = enriched["exit_reason"].fillna("").str.lower()
        pnl = enriched["pnl"].fillna(0.0)
        quick_cut = float(enriched["duration_minutes"].quantile(0.30)) if len(enriched) > 1 else 30.0
        quick_cut = max(1.0, quick_cut)

        stop_dist = (enriched["entry_price"] - enriched["stop_loss"]).abs() / enriched["entry_price"].replace(0, np.nan)
        short_stop_cut = float(stop_dist.quantile(0.35)) if stop_dist.notna().any() else 0.0

        enriched["ganho_rapido"] = (pnl > 0) & (enriched["duration_minutes"] <= quick_cut)
        enriched["perda_rapida"] = (pnl < 0) & (enriched["duration_minutes"] <= quick_cut)
        enriched["stop_curto"] = reason.str.contains("stop") & (stop_dist <= short_stop_cut)
        enriched["take_atingido"] = reason.str.contains("take")
        enriched["stop_por_volatilidade"] = reason.str.contains("trailing") | (
            reason.str.contains("stop") & (enriched["entry_vol_regime"] == "alta_volatilidade")
        )

        reversal_thr = 0.0015
        enriched["reversao"] = (enriched["first_move"] * enriched["final_move"] < 0) & (enriched["first_move"].abs() >= reversal_thr)
        enriched["continuacao"] = (enriched["first_move"] * enriched["final_move"] > 0) & (enriched["first_move"].abs() >= reversal_thr)

        false_breakout = (
            enriched["entry_is_breakout"].fillna(False)
            & (pnl < 0)
            & (enriched["first_move"] < -reversal_thr)
        )
        enriched["falso_rompimento"] = false_breakout
        enriched["rompimento"] = enriched["entry_is_breakout"].fillna(False) & (~false_breakout)

        enriched["regime_tendencia_forte_alta"] = enriched["entry_trend_regime"] == "tendencia_forte_alta"
        enriched["regime_tendencia_moderada_alta"] = enriched["entry_trend_regime"] == "tendencia_moderada_alta"
        enriched["regime_tendencia_forte_baixa"] = enriched["entry_trend_regime"] == "tendencia_forte_baixa"
        enriched["regime_tendencia_moderada_baixa"] = enriched["entry_trend_regime"] == "tendencia_moderada_baixa"
        enriched["regime_consolidacao"] = enriched["entry_trend_regime"] == "consolidacao"
        enriched["regime_alta_volatilidade"] = enriched["entry_vol_regime"] == "alta_volatilidade"
        enriched["regime_baixa_volatilidade"] = enriched["entry_vol_regime"] == "baixa_volatilidade"
        enriched["regime_rompimento"] = enriched["rompimento"]
        enriched["regime_falso_rompimento"] = enriched["falso_rompimento"]

        primary: list[str] = []
        for _, row in enriched.iterrows():
            if bool(row["falso_rompimento"]):
                primary.append("falso_rompimento")
            elif bool(row["rompimento"]):
                primary.append("rompimento")
            elif row["entry_trend_regime"] == "tendencia_forte_alta":
                primary.append("tendencia_forte_alta")
            elif row["entry_trend_regime"] == "tendencia_moderada_alta":
                primary.append("tendencia_moderada_alta")
            elif row["entry_trend_regime"] == "tendencia_forte_baixa":
                primary.append("tendencia_forte_baixa")
            elif row["entry_trend_regime"] == "tendencia_moderada_baixa":
                primary.append("tendencia_moderada_baixa")
            else:
                primary.append("consolidacao")
        enriched["primary_regime"] = primary

        profile_primary: list[str] = []
        for _, row in enriched.iterrows():
            if bool(row["take_atingido"]):
                profile_primary.append("take_atingido")
            elif bool(row["stop_por_volatilidade"]):
                profile_primary.append("stop_por_volatilidade")
            elif bool(row["stop_curto"]):
                profile_primary.append("stop_curto")
            elif bool(row["falso_rompimento"]):
                profile_primary.append("falso_rompimento")
            elif bool(row["reversao"]):
                profile_primary.append("reversao")
            elif bool(row["continuacao"]):
                profile_primary.append("continuacao")
            elif bool(row["ganho_rapido"]):
                profile_primary.append("ganho_rapido")
            elif bool(row["perda_rapida"]):
                profile_primary.append("perda_rapida")
            else:
                profile_primary.append("outros")
        enriched["primary_profile"] = profile_primary

        return enriched

    def _stage1_autopsy(self, df: pd.DataFrame) -> dict[str, Any]:
        losses = df[df["pnl"] < 0].copy()
        gains = df[df["pnl"] > 0].copy()

        total_loss_abs = float(losses["pnl"].abs().sum()) if not losses.empty else 0.0
        total_gain_abs = float(gains["pnl"].sum()) if not gains.empty else 0.0

        regime_cols = [
            "regime_tendencia_forte_alta",
            "regime_tendencia_moderada_alta",
            "regime_tendencia_forte_baixa",
            "regime_tendencia_moderada_baixa",
            "regime_consolidacao",
            "regime_alta_volatilidade",
            "regime_baixa_volatilidade",
            "regime_rompimento",
            "regime_falso_rompimento",
        ]

        loss_by_regime: list[dict[str, Any]] = []
        for col in regime_cols:
            subset = losses[losses[col]]
            loss_abs = float(subset["pnl"].abs().sum()) if not subset.empty else 0.0
            share = (loss_abs / total_loss_abs * 100.0) if total_loss_abs > 0 else 0.0
            loss_by_regime.append(
                {
                    "regime": col.replace("regime_", ""),
                    "loss_abs": round(loss_abs, 6),
                    "loss_share_pct": round(share, 6),
                    "loss_trades": int(len(subset)),
                }
            )
        loss_by_regime.sort(key=lambda x: x["loss_share_pct"], reverse=True)

        return {
            "sample_size": int(len(df)),
            "loss_trades": int(len(losses)),
            "gain_trades": int(len(gains)),
            "gross_loss_abs": round(total_loss_abs, 6),
            "gross_gain_abs": round(total_gain_abs, 6),
            "loss_distribution_by_regime": loss_by_regime,
            "objective_answers": {
                "onde_ocorre_maior_parte_das_perdas": loss_by_regime[0]["regime"] if loss_by_regime else None,
                "perdas_em_mercado_lateral_pct": self._regime_loss_share(loss_by_regime, "consolidacao"),
                "perdas_em_tendencia_forte_alta_pct": self._regime_loss_share(loss_by_regime, "tendencia_forte_alta"),
                "perdas_em_tendencia_moderada_alta_pct": self._regime_loss_share(loss_by_regime, "tendencia_moderada_alta"),
                "perdas_em_tendencia_forte_baixa_pct": self._regime_loss_share(loss_by_regime, "tendencia_forte_baixa"),
                "perdas_em_tendencia_moderada_baixa_pct": self._regime_loss_share(loss_by_regime, "tendencia_moderada_baixa"),
                "perdas_em_alta_volatilidade_pct": self._regime_loss_share(loss_by_regime, "alta_volatilidade"),
                "perdas_em_baixa_volatilidade_pct": self._regime_loss_share(loss_by_regime, "baixa_volatilidade"),
                "perdas_apos_rompimento_pct": self._regime_loss_share(loss_by_regime, "rompimento"),
                "perdas_apos_falso_rompimento_pct": self._regime_loss_share(loss_by_regime, "falso_rompimento"),
                "perdas_apos_reversao_pct": round(float((losses["reversao"].mean() * 100.0) if not losses.empty else 0.0), 6),
            },
        }

    def _regime_loss_share(self, rows: list[dict[str, Any]], name: str) -> float:
        for row in rows:
            if row["regime"] == name:
                return float(row["loss_share_pct"])
        return 0.0

    def _stage2_profile_distribution(self, df: pd.DataFrame) -> dict[str, Any]:
        profile_cols = [
            "ganho_rapido",
            "perda_rapida",
            "stop_curto",
            "take_atingido",
            "stop_por_volatilidade",
            "rompimento",
            "falso_rompimento",
            "reversao",
            "continuacao",
        ]

        total = max(int(len(df)), 1)
        dist = []
        for col in profile_cols:
            count = int(df[col].sum())
            dist.append(
                {
                    "profile": col,
                    "count": count,
                    "distribution_pct": round(count / total * 100.0, 6),
                }
            )

        primary = (
            df.groupby("primary_profile", dropna=False)
            .agg(
                trades=("operation_id", "count"),
                net_pnl=("pnl", "sum"),
                avg_pnl=("pnl", "mean"),
                win_rate=("pnl", lambda s: float((s > 0).mean() * 100.0)),
            )
            .reset_index()
            .sort_values("trades", ascending=False)
        )

        return {
            "binary_profile_distribution": dist,
            "primary_profile_distribution": primary.to_dict(orient="records"),
        }

    def _stage3_regime_statistics(self, df: pd.DataFrame) -> dict[str, Any]:
        regime_map = {
            "tendencia_forte_alta": "regime_tendencia_forte_alta",
            "tendencia_moderada_alta": "regime_tendencia_moderada_alta",
            "tendencia_forte_baixa": "regime_tendencia_forte_baixa",
            "tendencia_moderada_baixa": "regime_tendencia_moderada_baixa",
            "consolidacao": "regime_consolidacao",
            "alta_volatilidade": "regime_alta_volatilidade",
            "baixa_volatilidade": "regime_baixa_volatilidade",
            "rompimento": "regime_rompimento",
            "falso_rompimento": "regime_falso_rompimento",
        }

        total_loss_abs = float(df.loc[df["pnl"] < 0, "pnl"].abs().sum())
        total_gain_abs = float(df.loc[df["pnl"] > 0, "pnl"].sum())
        total_loss_abs = max(total_loss_abs, 1e-9)
        total_gain_abs = max(total_gain_abs, 1e-9)

        rows: list[dict[str, Any]] = []
        for regime_name, col in regime_map.items():
            subset = df[df[col]].copy()
            if subset.empty:
                rows.append(
                    {
                        "regime": regime_name,
                        "trades": 0,
                        "loss_share_pct": 0.0,
                        "profit_share_pct": 0.0,
                        "profit_factor": None,
                        "sharpe": None,
                        "expectancy": None,
                        "drawdown": None,
                        "win_rate": None,
                        "avg_trades_per_day": None,
                        "avg_duration_minutes": None,
                    }
                )
                continue

            pnl_series = subset["pnl"].astype(float)
            gross_profit = float(subset.loc[subset["pnl"] > 0, "pnl"].sum())
            gross_loss = float(abs(subset.loc[subset["pnl"] < 0, "pnl"].sum()))
            profit_factor = profit_factor_from_pnl(pnl_series)
            sharpe = sharpe_from_pnl(pnl_series)
            expectancy = expectancy_from_pnl(pnl_series)
            win_rate = win_rate_from_pnl(pnl_series) * 100.0
            avg_duration = float(subset["duration_minutes"].mean())
            drawdown = max_drawdown_from_pnl(pnl_series)

            ordered = subset.sort_values("exit_time").copy()
            span_days = max((ordered["exit_time"].max() - ordered["entry_time"].min()).days, 1)
            trades_per_day = float(len(subset) / span_days)

            rows.append(
                {
                    "regime": regime_name,
                    "trades": int(len(subset)),
                    "loss_share_pct": round(float(gross_loss / total_loss_abs * 100.0), 6),
                    "profit_share_pct": round(float(gross_profit / total_gain_abs * 100.0), 6),
                    "profit_factor": None if not np.isfinite(float(profit_factor)) else round(float(profit_factor), 6),
                    "sharpe": round(float(sharpe), 6),
                    "expectancy": round(expectancy, 6),
                    "drawdown": round(drawdown, 6),
                    "win_rate": round(win_rate, 6),
                    "avg_trades_per_day": round(trades_per_day, 6),
                    "avg_duration_minutes": round(avg_duration, 6),
                }
            )

        rows.sort(key=lambda x: x["loss_share_pct"], reverse=True)

        by_strategy = (
            df.groupby(["strategy", "primary_regime"], dropna=False)
            .agg(trades=("operation_id", "count"), net_pnl=("pnl", "sum"), win_rate=("pnl", lambda s: float((s > 0).mean() * 100.0)))
            .reset_index()
            .sort_values(["strategy", "net_pnl"], ascending=[True, False])
        )

        return {
            "statistics_by_regime": rows,
            "strategy_x_regime": by_strategy.to_dict(orient="records"),
        }

    def _stage5_hypothesis_tests(self, df: pd.DataFrame, stage3: dict[str, Any]) -> dict[str, Any]:
        stats = pd.DataFrame(stage3.get("statistics_by_regime", []))
        if stats.empty:
            return {"results": []}

        def _metric(regime: str, key: str, default: float = 0.0) -> float:
            row = stats.loc[stats["regime"] == regime]
            if row.empty:
                return default
            value = row.iloc[0].get(key)
            if value is None or (isinstance(value, float) and np.isnan(value)):
                return default
            return float(value)

        overall_expectancy = float(df["pnl"].mean())
        overall_win = float((df["pnl"] > 0).mean() * 100.0)
        gp = float(df.loc[df["pnl"] > 0, "pnl"].sum())
        gl = float(abs(df.loc[df["pnl"] < 0, "pnl"].sum()))
        overall_pf = gp / gl if gl > 0 else 0.0

        hypotheses: list[dict[str, Any]] = []

        # H1
        cons_loss = _metric("consolidacao", "loss_share_pct")
        trend_losses = [
            _metric("tendencia_forte_alta", "loss_share_pct"),
            _metric("tendencia_moderada_alta", "loss_share_pct"),
            _metric("tendencia_forte_baixa", "loss_share_pct"),
            _metric("tendencia_moderada_baixa", "loss_share_pct"),
        ]
        cons_pf = _metric("consolidacao", "profit_factor")
        cons_exp = _metric("consolidacao", "expectancy")
        cons_wr = _metric("consolidacao", "win_rate")
        cons_trades = _metric("consolidacao", "trades")

        favorable_h1 = [
            cons_loss >= max(trend_losses) if trend_losses else False,
            cons_pf < overall_pf,
            cons_exp < overall_expectancy,
            cons_wr < overall_win,
            cons_trades >= 0.15 * len(df),
        ]
        contrary_h1 = [
            cons_loss < np.mean(trend_losses) if trend_losses else False,
            cons_pf >= overall_pf,
            cons_exp >= overall_expectancy,
            cons_wr >= overall_win,
            cons_trades < 0.10 * len(df),
        ]

        hypotheses.append(
            self._build_hypothesis_result(
                hypothesis_id="H1",
                statement="A estrategia perde por operar em consolidacao sem filtro de regime robusto.",
                favorable=favorable_h1,
                contrary=contrary_h1,
                impact_expected=cons_loss,
                sample_size=int(len(df)),
                min_sample_confirm=200,
                evidence={
                    "consolidacao_loss_share_pct": round(cons_loss, 6),
                    "consolidacao_profit_factor": round(cons_pf, 6),
                    "consolidacao_expectancy": round(cons_exp, 6),
                    "consolidacao_win_rate": round(cons_wr, 6),
                    "consolidacao_trades": int(cons_trades),
                },
            )
        )

        # H2
        high_vol_loss = _metric("alta_volatilidade", "loss_share_pct")
        high_vol_pf = _metric("alta_volatilidade", "profit_factor")
        low_vol_pf = _metric("baixa_volatilidade", "profit_factor")

        favorable_h2 = [
            high_vol_loss >= 35.0,
            high_vol_pf < low_vol_pf if low_vol_pf > 0 else True,
        ]
        contrary_h2 = [
            high_vol_loss < 25.0,
            high_vol_pf >= low_vol_pf if low_vol_pf > 0 else False,
        ]

        hypotheses.append(
            self._build_hypothesis_result(
                hypothesis_id="H2",
                statement="A estrategia perde de forma desproporcional em alta volatilidade.",
                favorable=favorable_h2,
                contrary=contrary_h2,
                impact_expected=high_vol_loss,
                sample_size=int(len(df)),
                min_sample_confirm=200,
                evidence={
                    "alta_vol_loss_share_pct": round(high_vol_loss, 6),
                    "alta_vol_profit_factor": round(high_vol_pf, 6),
                    "baixa_vol_profit_factor": round(low_vol_pf, 6),
                },
            )
        )

        # H3
        false_break_loss = _metric("falso_rompimento", "loss_share_pct")
        false_break_wr = _metric("falso_rompimento", "win_rate")

        favorable_h3 = [
            false_break_loss >= 15.0,
            false_break_wr < overall_win,
        ]
        contrary_h3 = [
            false_break_loss < 8.0,
            false_break_wr >= overall_win,
        ]

        hypotheses.append(
            self._build_hypothesis_result(
                hypothesis_id="H3",
                statement="Rompimentos falsos estao entre as principais fontes de perda.",
                favorable=favorable_h3,
                contrary=contrary_h3,
                impact_expected=false_break_loss,
                sample_size=int(len(df)),
                min_sample_confirm=150,
                evidence={
                    "falso_rompimento_loss_share_pct": round(false_break_loss, 6),
                    "falso_rompimento_win_rate": round(false_break_wr, 6),
                },
            )
        )

        return {"results": hypotheses}

    def _build_hypothesis_result(
        self,
        hypothesis_id: str,
        statement: str,
        favorable: list[bool],
        contrary: list[bool],
        impact_expected: float,
        sample_size: int,
        min_sample_confirm: int,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        favor = int(sum(1 for item in favorable if item))
        contra = int(sum(1 for item in contrary if item))
        total = max(favor + contra, 1)
        confidence = favor / total * 100.0

        if sample_size >= min_sample_confirm and confidence >= 70.0 and favor >= 3:
            status = "CONFIRMADA"
        elif confidence < 45.0 and contra >= favor:
            status = "REJEITADA"
        else:
            status = "INCONCLUSIVA"

        return {
            "id": hypothesis_id,
            "hypothesis": statement,
            "evidencias_favoraveis": favor,
            "evidencias_contrarias": contra,
            "nivel_confianca_pct": round(confidence, 6),
            "impacto_esperado_pct": round(float(impact_expected), 6),
            "sample_size": sample_size,
            "classification": status,
            "evidence": evidence,
        }

    def _stage6_market_regime_detector_design(self, stage5: dict[str, Any]) -> dict[str, Any]:
        h1_status = self._find_hypothesis_status(stage5, "H1")
        if h1_status != "CONFIRMADA":
            return {
                "status": "SKIPPED",
                "reason": "H1 nao confirmada; detector de regime nao deve ser desenhado para integracao ainda.",
            }

        design = {
            "status": "DESIGNED",
            "component_name": "Market Regime Detector",
            "objective": "Classificar o mercado antes de permitir entradas para bloquear consolidacao desfavoravel.",
            "inputs": [
                "OHLCV em tempo real",
                "ATR% rolling",
                "EMA fast/slow e trend_score",
                "breakout score",
            ],
            "outputs": [
                "trend_regime granular (forte/moderada alta/baixa, consolidacao)",
                "vol_regime (alta/baixa)",
                "breakout_state (rompimento/falso rompimento)",
                "allow_entry boolean",
                "confidence_score 0..1",
            ],
            "decision_rules": [
                "Bloquear entrada quando regime=consolidacao e confidence_score baixo",
                "Reduzir exposicao em alta volatilidade com falso rompimento recente",
                "Permitir entrada apenas em regimes com expectativa historica positiva",
            ],
            "validation_plan": [
                "A/B test com e sem detector",
                "Comparar PF/expectancy por regime alvo",
                "Medir queda de perdas em consolidacao",
            ],
        }

        out = self._base_dir / "optimization" / "results" / "market_regime_detector_design.md"
        lines = [
            "# Market Regime Detector (Design)",
            "",
            f"Generated at: {datetime.now(timezone.utc).isoformat()}",
            "",
            "## Objetivo",
            design["objective"],
            "",
            "## Inputs",
        ]
        for item in design["inputs"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("## Outputs")
        for item in design["outputs"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("## Regras de Decisao")
        for item in design["decision_rules"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("## Plano de Validacao")
        for item in design["validation_plan"]:
            lines.append(f"- {item}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

        design["artifact"] = str(out)
        return design

    def _stage7_trendv3_gate(self, stage5: dict[str, Any]) -> dict[str, Any]:
        h1_status = self._find_hypothesis_status(stage5, "H1")
        if h1_status != "CONFIRMADA":
            return {
                "status": "BLOCKED",
                "reason": "TrendV3 bloqueada: H1 nao confirmada estatisticamente.",
            }

        blueprint = {
            "status": "ALLOWED",
            "rule": "TrendV3 deve mapear cada mudanca a uma hipotese confirmada.",
            "required_questions": [
                "Qual hipotese confirmada esta sendo atacada?",
                "Qual evidencia quantitativa motivou a mudanca?",
                "Como a validacao vai confirmar ou rejeitar a hipotese?",
            ],
        }
        return blueprint

    def _find_hypothesis_status(self, stage5: dict[str, Any], hypothesis_id: str) -> str | None:
        for item in stage5.get("results", []):
            if item.get("id") == hypothesis_id:
                return item.get("classification")
        return None

    def _build_final_report(
        self,
        source: str,
        reconstruction_meta: dict[str, Any],
        consolidated: pd.DataFrame,
        stage3: dict[str, Any],
        stage5: dict[str, Any],
        stage6: dict[str, Any],
        stage7: dict[str, Any],
    ) -> dict[str, Any]:
        total_ops = int(len(consolidated))
        candidates_reconstructed = {
            key: int(value.get("selected", 0)) for key, value in reconstruction_meta.items() if isinstance(value, dict)
        }

        by_asset = (
            consolidated.groupby("symbol", dropna=False)
            .size()
            .reset_index(name="operations")
            .sort_values("operations", ascending=False)
            .to_dict(orient="records")
        )
        by_tf = (
            consolidated.groupby("timeframe", dropna=False)
            .size()
            .reset_index(name="operations")
            .sort_values("operations", ascending=False)
            .to_dict(orient="records")
        )

        confirmed = [item for item in stage5.get("results", []) if item.get("classification") == "CONFIRMADA"]
        rejected = [item for item in stage5.get("results", []) if item.get("classification") == "REJEITADA"]
        inconclusive = [item for item in stage5.get("results", []) if item.get("classification") == "INCONCLUSIVA"]

        h1_status = self._find_hypothesis_status(stage5, "H1")
        recommendation = "Criar TrendV3." if h1_status == "CONFIRMADA" else "Coletar mais evidencias antes de alterar a estrategia."

        return {
            "total_operations_analyzed": total_ops,
            "candidates_reconstructed": candidates_reconstructed,
            "distribution_by_asset": by_asset,
            "distribution_by_timeframe": by_tf,
            "statistics_by_regime": stage3.get("statistics_by_regime", []),
            "hypotheses_confirmed": confirmed,
            "hypotheses_rejected": rejected,
            "hypotheses_inconclusive": inconclusive,
            "recommendation": recommendation,
            "h1_status": h1_status,
            "trade_source": source,
            "stage6_status": stage6.get("status"),
            "stage7_status": stage7.get("status"),
        }

    def _write_outputs(self, payload: dict[str, Any], operations: pd.DataFrame, candles: pd.DataFrame) -> dict[str, str]:
        out = self._base_dir / "optimization" / "results"
        out.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = out / f"strategy_research_lab_{stamp}.json"
        md_path = out / f"strategy_research_lab_{stamp}.md"
        ops_csv = out / f"strategy_research_operations_{stamp}.csv"
        candles_csv = out / f"strategy_research_regimes_{stamp}.csv"
        stage5_csv = out / f"strategy_research_hypothesis_tests_{stamp}.csv"

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        operations.to_csv(ops_csv, index=False)
        candles.to_csv(candles_csv, index=False)
        pd.DataFrame(payload.get("stage5_hypothesis_tests", {}).get("results", [])).to_csv(stage5_csv, index=False)

        fr = payload.get("final_report", {})
        lines = [
            "# Strategy Research Lab - Statistical Confirmation",
            "",
            f"Generated at: {payload.get('generated_at')}",
            "",
            "## Relatorio Final",
            f"1. Quantidade total de operacoes analisadas: {fr.get('total_operations_analyzed')}",
            f"2. Quantidade de candidatos reconstruidos: {fr.get('candidates_reconstructed')}",
            f"3. Distribuicao por ativo: {fr.get('distribution_by_asset')}",
            f"4. Distribuicao por timeframe: {fr.get('distribution_by_timeframe')}",
            f"5. Estatisticas por regime de mercado: {fr.get('statistics_by_regime')}",
            f"6. Hipoteses confirmadas: {fr.get('hypotheses_confirmed')}",
            f"7. Hipoteses rejeitadas: {fr.get('hypotheses_rejected')}",
            f"8. Hipoteses inconclusivas: {fr.get('hypotheses_inconclusive')}",
            f"9. Recomendacao objetiva: {fr.get('recommendation')}",
            "",
            "## Gate",
            f"- Stage6: {fr.get('stage6_status')}",
            f"- Stage7: {fr.get('stage7_status')}",
        ]
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return {
            "research_json": str(json_path),
            "research_markdown": str(md_path),
            "operations_csv": str(ops_csv),
            "regimes_csv": str(candles_csv),
            "hypothesis_tests_csv": str(stage5_csv),
        }

