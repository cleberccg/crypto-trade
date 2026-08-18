from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from database.repositories import CandleRepository
from utils.metrics import (
    capture_ratio_from_realized_and_mfe,
    expectancy_from_pnl,
    max_drawdown_from_pnl,
    mean_capture_ratio,
    profit_factor_from_pnl,
    recovery_factor_from_pnl,
    sharpe_from_pnl,
    win_rate_from_pnl,
)
from utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class TradeManagementLabConfig:
    operations_csv: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    max_bars: int = 96
    atr_period: int = 14
    atr_mult: float = 2.0
    time_stop_bars: int = 24
    momentum_fast: int = 8
    momentum_slow: int = 21
    mfe_pullback_ratio: float = 0.35
    bootstrap_iterations: int = 500


class TradeManagementResearchLab:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, config: TradeManagementLabConfig) -> dict[str, Any]:
        t0 = datetime.now(timezone.utc)
        operations = self._load_operations(config)
        if operations.empty:
            raise ValueError("Nenhuma operacao encontrada para o Trade Management Lab.")
        logger.info("TradeManagementLab — operacoes carregadas: %d", len(operations))

        tf_minutes = self._timeframe_to_minutes(str(operations["timeframe"].mode().iloc[0]))
        candles_by_market = self._load_candles_for_operations(operations, tf_minutes, config.max_bars)

        scenario_rows: list[dict[str, Any]] = []
        total_ops = len(operations)
        for idx, (_, trade) in enumerate(operations.sort_values("entry_time").iterrows(), start=1):
            symbol = str(trade["symbol"])
            timeframe = str(trade["timeframe"])
            key = (symbol, timeframe)
            market = candles_by_market.get(key)
            if market is None or market.empty:
                continue

            path = self._extract_trade_path(trade, market, tf_minutes, config.max_bars)
            if path is None:
                continue

            for scenario in ["A", "B", "C", "D", "E", "F", "G"]:
                sim = self._simulate_scenario(trade, path, scenario, config)
                scenario_rows.append(
                    {
                        "operation_id": str(trade["operation_id"]),
                        "strategy": str(trade["strategy"]),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "scenario": scenario,
                        **sim,
                    }
                )
            if idx % 20 == 0 or idx == total_ops:
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                rate = idx / elapsed if elapsed > 0 else 0.0
                eta = (total_ops - idx) / rate if rate > 0 else 0.0
                logger.info(
                    "TradeManagementLab — %d/%d (%.1f%%) | cenarios=%d | ETA %.1fs",
                    idx,
                    total_ops,
                    idx / total_ops * 100,
                    len(scenario_rows),
                    eta,
                )

        scenarios_df = pd.DataFrame(scenario_rows)
        if scenarios_df.empty:
            raise ValueError("Nao foi possivel simular cenarios de gestao nas operacoes carregadas.")

        metrics = self._compute_metrics_by_scenario(scenarios_df)
        significance = self._significance_vs_baseline(scenarios_df, int(config.bootstrap_iterations))
        ranking = self._rank_scenarios(metrics)
        diagnosis = self._build_diagnosis(metrics, significance, ranking)

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "operations_csv": config.operations_csv,
                "symbol": config.symbol,
                "timeframe": config.timeframe,
                "max_bars": config.max_bars,
                "atr_period": config.atr_period,
                "atr_mult": config.atr_mult,
                "time_stop_bars": config.time_stop_bars,
                "momentum_fast": config.momentum_fast,
                "momentum_slow": config.momentum_slow,
                "mfe_pullback_ratio": config.mfe_pullback_ratio,
                "bootstrap_iterations": config.bootstrap_iterations,
            },
            "sample_size": int(operations.shape[0]),
            "scenario_metrics": metrics.to_dict(orient="records"),
            "significance": significance,
            "ranking": ranking,
            "diagnosis": diagnosis,
        }

        outputs = self._write_outputs(payload, scenarios_df, metrics)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        logger.info("TradeManagementLab — concluido em %.2fs | sample_size=%d", elapsed, int(operations.shape[0]))

        return {
            "summary": {
                "sample_size": int(operations.shape[0]),
                "best_pf_scenario": str(metrics.sort_values("profit_factor", ascending=False).iloc[0]["scenario"]),
                "best_sharpe_scenario": str(metrics.sort_values("sharpe", ascending=False).iloc[0]["scenario"]),
                "diagnostic_option": diagnosis["decision_option"],
                "recommendation": diagnosis["recommendation"],
            },
            "outputs": outputs,
        }

    def _load_operations(self, config: TradeManagementLabConfig) -> pd.DataFrame:
        if config.operations_csv:
            csv_path = Path(config.operations_csv)
        else:
            candidates = sorted(
                (self._base_dir / "optimization" / "results").glob("strategy_research_operations_*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise ValueError("Nenhum arquivo strategy_research_operations_*.csv encontrado.")
            csv_path = candidates[0]

        df = pd.read_csv(csv_path)
        if df.empty:
            return df

        for col in ["entry_time", "exit_time"]:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        for col in ["entry_price", "final_move", "mfe", "mae", "pnl", "quantity", "duration_minutes"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if config.symbol:
            df = df[df["symbol"] == config.symbol]
        if config.timeframe:
            df = df[df["timeframe"] == config.timeframe]

        return df.dropna(subset=["entry_time", "entry_price"]).reset_index(drop=True)

    def _load_candles_for_operations(
        self,
        operations: pd.DataFrame,
        tf_minutes: int,
        max_bars: int,
    ) -> dict[tuple[str, str], pd.DataFrame]:
        repo = CandleRepository(self._session)
        out: dict[tuple[str, str], pd.DataFrame] = {}

        by_market = operations.groupby(["symbol", "timeframe"], dropna=False)
        extra_minutes = max(1, tf_minutes) * (max_bars + 10)

        for (symbol, timeframe), group in by_market:
            start = pd.to_datetime(group["entry_time"].min(), utc=True).to_pydatetime() - timedelta(days=3)
            end = pd.to_datetime(group["entry_time"].max(), utc=True).to_pydatetime() + timedelta(minutes=extra_minutes)

            rows = repo.get_range(symbol=str(symbol), timeframe=str(timeframe), start=start, end=end)
            if not rows and "/" in str(symbol):
                rows = repo.get_range(symbol=str(symbol).replace("/", ""), timeframe=str(timeframe), start=start, end=end)
            if not rows:
                continue

            frame = pd.DataFrame(
                [
                    {
                        "timestamp": item.open_time,
                        "open": item.open,
                        "high": item.high,
                        "low": item.low,
                        "close": item.close,
                    }
                    for item in rows
                ]
            )
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            frame = frame.sort_values("timestamp").reset_index(drop=True)

            prev_close = frame["close"].shift(1)
            tr = pd.concat(
                [
                    (frame["high"] - frame["low"]).abs(),
                    (frame["high"] - prev_close).abs(),
                    (frame["low"] - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            frame["atr14"] = tr.rolling(14, min_periods=5).mean().bfill().fillna(0.0)
            out[(str(symbol), str(timeframe))] = frame

        return out

    def _extract_trade_path(
        self,
        trade: pd.Series,
        market: pd.DataFrame,
        tf_minutes: int,
        max_bars: int,
    ) -> dict[str, Any] | None:
        entry_ts = pd.to_datetime(trade["entry_time"], utc=True)
        if pd.isna(entry_ts):
            return None

        slice_df = market[market["timestamp"] >= entry_ts].head(max_bars)
        if slice_df.empty:
            return None

        entry_price = float(trade["entry_price"])
        side = -1.0 if float(trade.get("quantity", 1.0) or 1.0) < 0 else 1.0

        if side > 0:
            ret_high = (slice_df["high"].to_numpy() - entry_price) / entry_price
            ret_low = (slice_df["low"].to_numpy() - entry_price) / entry_price
            ret_close = (slice_df["close"].to_numpy() - entry_price) / entry_price
        else:
            ret_high = (entry_price - slice_df["low"].to_numpy()) / entry_price
            ret_low = (entry_price - slice_df["high"].to_numpy()) / entry_price
            ret_close = (entry_price - slice_df["close"].to_numpy()) / entry_price

        return {
            "timestamps": slice_df["timestamp"].to_numpy(),
            "ret_high": ret_high,
            "ret_low": ret_low,
            "ret_close": ret_close,
            "atr": slice_df["atr14"].to_numpy() / entry_price,
            "tf_minutes": tf_minutes,
            "entry_price": entry_price,
        }

    def _simulate_scenario(
        self,
        trade: pd.Series,
        path: dict[str, Any],
        scenario: str,
        config: TradeManagementLabConfig,
    ) -> dict[str, Any]:
        ret_high = path["ret_high"]
        ret_low = path["ret_low"]
        ret_close = path["ret_close"]
        atr = path["atr"]

        risk = float(abs(trade.get("mae", np.nan)))
        if not np.isfinite(risk) or risk <= 0:
            risk = max(float(np.nanpercentile(np.abs(ret_low), 60)), 1e-4)

        actual_final = float(trade.get("final_move", 0.0) or 0.0)
        actual_duration_min = float(trade.get("duration_minutes", np.nan))
        default_exit_idx = len(ret_close) - 1

        def _finish(exit_idx: int, realized_return: float) -> dict[str, float]:
            exit_idx = max(0, min(exit_idx, len(ret_close) - 1))
            notional = abs(float(trade.get("quantity", 1.0) or 1.0) * float(trade.get("entry_price", 1.0) or 1.0))
            if not np.isfinite(notional) or notional <= 0:
                notional = 1.0
            pnl = realized_return * notional
            path_mfe = float(np.nanmax(ret_high))
            capture_ratio = capture_ratio_from_realized_and_mfe(realized_return, path_mfe)
            mae_until_exit = float(np.nanmin(ret_low[: exit_idx + 1]))
            duration_minutes = float((exit_idx + 1) * path["tf_minutes"])
            return {
                "realized_return": float(realized_return),
                "pnl": float(pnl),
                "exit_bars": int(exit_idx + 1),
                "duration_minutes": duration_minutes,
                "path_mfe": path_mfe,
                "path_mae": float(np.nanmin(ret_low)),
                "mae_until_exit": mae_until_exit,
                "capture_ratio": float(capture_ratio) if np.isfinite(capture_ratio) else np.nan,
                "mfe_used_pct": float(capture_ratio * 100.0) if np.isfinite(capture_ratio) else np.nan,
            }

        if scenario == "A":
            if np.isfinite(actual_duration_min):
                exit_idx = min(default_exit_idx, max(0, int(round(actual_duration_min / path["tf_minutes"])) - 1))
            else:
                exit_idx = default_exit_idx
            return _finish(exit_idx, actual_final)

        if scenario == "B":
            stop = -risk
            armed = False
            for i in range(len(ret_close)):
                if not armed and ret_high[i] >= risk:
                    armed = True
                stop_now = 0.0 if armed else stop
                if ret_low[i] <= stop_now:
                    return _finish(i, stop_now)
            return _finish(default_exit_idx, float(ret_close[-1]))

        if scenario == "C":
            stop = -risk
            for i in range(len(ret_close)):
                trail = ret_high[i] - max(1e-6, config.atr_mult * atr[i])
                stop = max(stop, trail)
                if ret_low[i] <= stop:
                    return _finish(i, stop)
            return _finish(default_exit_idx, float(ret_close[-1]))

        if scenario == "D":
            first_half = False
            runner_stop = -risk
            realized = 0.0
            for i in range(len(ret_close)):
                if not first_half and ret_high[i] >= risk:
                    first_half = True
                    realized += 0.5 * risk
                    runner_stop = max(0.0, runner_stop)

                if first_half:
                    trail = ret_high[i] - max(1e-6, config.atr_mult * atr[i])
                    runner_stop = max(runner_stop, trail)
                    if ret_low[i] <= runner_stop:
                        realized += 0.5 * runner_stop
                        return _finish(i, realized)
                else:
                    if ret_low[i] <= -risk:
                        return _finish(i, -risk)

            if first_half:
                realized += 0.5 * float(ret_close[-1])
                return _finish(default_exit_idx, realized)
            return _finish(default_exit_idx, float(ret_close[-1]))

        if scenario == "E":
            stop = -risk
            peak = -1e9
            for i in range(len(ret_close)):
                peak = max(peak, ret_high[i])
                dynamic_pullback = config.mfe_pullback_ratio * max(peak, risk)
                stop = max(stop, peak - dynamic_pullback)
                if ret_low[i] <= stop:
                    return _finish(i, stop)
            return _finish(default_exit_idx, float(ret_close[-1]))

        if scenario == "F":
            idx = min(default_exit_idx, max(0, int(config.time_stop_bars) - 1))
            return _finish(idx, float(ret_close[idx]))

        # Scenario G: momentum loss with fast/slow EMA cross and protective stop.
        prices = (ret_close + 1.0) * path["entry_price"]
        ema_fast = pd.Series(prices).ewm(span=max(2, int(config.momentum_fast)), adjust=False).mean().to_numpy()
        ema_slow = pd.Series(prices).ewm(span=max(3, int(config.momentum_slow)), adjust=False).mean().to_numpy()
        stop = -risk
        for i in range(len(ret_close)):
            if ret_low[i] <= stop:
                return _finish(i, stop)
            if i >= 1 and ema_fast[i] < ema_slow[i]:
                return _finish(i, float(ret_close[i]))
        return _finish(default_exit_idx, float(ret_close[-1]))

    def _compute_metrics_by_scenario(self, scenarios_df: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for scenario, grp in scenarios_df.groupby("scenario", dropna=False):
            pnl = pd.to_numeric(grp["pnl"], errors="coerce").fillna(0.0)
            wins = pnl[pnl > 0]
            losses = pnl[pnl < 0]
            profit_factor = profit_factor_from_pnl(pnl)
            net_profit = float(pnl.sum())
            expectancy = expectancy_from_pnl(pnl)
            win_rate = win_rate_from_pnl(pnl) * 100.0
            avg_winner = float(wins.mean()) if not wins.empty else 0.0
            avg_loser = float(losses.mean()) if not losses.empty else 0.0
            sharpe = sharpe_from_pnl(pnl)
            max_drawdown = max_drawdown_from_pnl(pnl)
            recovery_factor = recovery_factor_from_pnl(pnl)

            capture_ratio = mean_capture_ratio(grp["capture_ratio"])
            mfe_used = float(pd.to_numeric(grp["mfe_used_pct"], errors="coerce").mean())
            mae = float(pd.to_numeric(grp["mae_until_exit"], errors="coerce").mean())
            avg_duration = float(pd.to_numeric(grp["duration_minutes"], errors="coerce").mean())

            rows.append(
                {
                    "scenario": str(scenario),
                    "trades": int(len(grp)),
                    "profit_factor": profit_factor,
                    "sharpe": sharpe,
                    "expectancy": expectancy,
                    "drawdown": max_drawdown,
                    "net_profit": net_profit,
                    "win_rate": win_rate,
                    "avg_winner": avg_winner,
                    "avg_loser": avg_loser,
                    "recovery_factor": recovery_factor,
                    "capture_ratio": capture_ratio,
                    "mfe_used_pct": mfe_used,
                    "mae": mae,
                    "avg_duration_minutes": avg_duration,
                }
            )

        return pd.DataFrame(rows).sort_values("scenario").reset_index(drop=True)

    def _significance_vs_baseline(self, scenarios_df: pd.DataFrame, iters: int) -> dict[str, Any]:
        baseline = scenarios_df[scenarios_df["scenario"] == "A"]
        base_by_id = baseline.set_index("operation_id")
        if base_by_id.empty:
            return {}

        rng = np.random.default_rng(42)
        out: dict[str, Any] = {}

        for scenario in sorted(scenarios_df["scenario"].unique()):
            if scenario == "A":
                continue
            cur = scenarios_df[scenarios_df["scenario"] == scenario].set_index("operation_id")
            merged = cur.join(base_by_id[["pnl"]], how="inner", rsuffix="_base")
            if merged.empty:
                continue

            diff = (pd.to_numeric(merged["pnl"], errors="coerce") - pd.to_numeric(merged["pnl_base"], errors="coerce")).to_numpy()
            diff = diff[np.isfinite(diff)]
            if diff.size == 0:
                continue

            means = []
            for _ in range(max(50, iters)):
                sample = rng.choice(diff, size=diff.size, replace=True)
                means.append(float(np.mean(sample)))
            lo = float(np.quantile(means, 0.025))
            hi = float(np.quantile(means, 0.975))

            out[str(scenario)] = {
                "mean_pnl_delta": float(np.mean(diff)),
                "ci95": [lo, hi],
                "significant_improvement": bool(lo > 0),
            }

        return out

    def _rank_scenarios(self, metrics: pd.DataFrame) -> list[dict[str, Any]]:
        work = metrics.copy()
        if work.empty:
            return []

        cols_high = [
            "profit_factor",
            "sharpe",
            "expectancy",
            "net_profit",
            "win_rate",
            "recovery_factor",
            "capture_ratio",
            "mfe_used_pct",
        ]
        cols_low = ["drawdown"]

        for col in cols_high:
            series = pd.to_numeric(work[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
            work[f"{col}_score"] = series.rank(pct=True)
        for col in cols_low:
            series = pd.to_numeric(work[col], errors="coerce")
            work[f"{col}_score"] = 1 - series.rank(pct=True)

        weighted = (
            0.20 * work["profit_factor_score"]
            + 0.15 * work["sharpe_score"]
            + 0.10 * work["expectancy_score"]
            + 0.15 * work["net_profit_score"]
            + 0.10 * work["win_rate_score"]
            + 0.10 * work["capture_ratio_score"]
            + 0.10 * work["mfe_used_pct_score"]
            + 0.10 * work["drawdown_score"]
        )
        work["ranking_score"] = weighted
        ranked = work.sort_values("ranking_score", ascending=False).reset_index(drop=True)

        payload: list[dict[str, Any]] = []
        for i, row in ranked.iterrows():
            payload.append(
                {
                    "rank": int(i + 1),
                    "scenario": str(row["scenario"]),
                    "ranking_score": float(row["ranking_score"]),
                }
            )
        return payload

    def _build_diagnosis(self, metrics: pd.DataFrame, significance: dict[str, Any], ranking: list[dict[str, Any]]) -> dict[str, Any]:
        if metrics.empty:
            return {
                "decision_option": "B",
                "recommendation": "Base insuficiente para comprovar melhoria de gestao.",
            }

        baseline = metrics[metrics["scenario"] == "A"]
        if baseline.empty:
            return {
                "decision_option": "B",
                "recommendation": "Sem baseline A para comparacao.",
            }

        base = baseline.iloc[0]
        best = metrics.sort_values(["profit_factor", "sharpe", "net_profit"], ascending=False).iloc[0]
        best_scenario = str(best["scenario"])
        sig = significance.get(best_scenario, {})
        sig_improve = bool(sig.get("significant_improvement", False))

        improved_pf = float(best["profit_factor"]) > float(base["profit_factor"])
        improved_sharpe = float(best["sharpe"]) > float(base["sharpe"])
        improved_net = float(best["net_profit"]) > float(base["net_profit"])
        improved_capture = float(best["capture_ratio"]) > float(base["capture_ratio"])

        if best_scenario != "A" and improved_pf and improved_sharpe and improved_net and improved_capture and sig_improve:
            option = "A"
            recommendation = (
                "Entradas mostram edge condicional; seguir para TrendV3 mantendo entradas atuais e adotando a gestao "
                f"do cenario {best_scenario}."
            )
        else:
            option = "B"
            recommendation = "Mesmo com gestoes alternativas, nao houve evidencia robusta de vantagem estatistica."

        return {
            "decision_option": option,
            "best_scenario": best_scenario,
            "improved_vs_baseline": {
                "profit_factor": improved_pf,
                "sharpe": improved_sharpe,
                "net_profit": improved_net,
                "capture_ratio": improved_capture,
                "statistically_significant": sig_improve,
            },
            "recommendation": recommendation,
            "ranking_top3": ranking[:3],
        }

    def _write_outputs(self, payload: dict[str, Any], scenarios_df: pd.DataFrame, metrics_df: pd.DataFrame) -> dict[str, str]:
        out = self._base_dir / "optimization" / "results"
        out.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = out / f"trade_management_lab_{stamp}.json"
        md_path = out / f"trade_management_lab_{stamp}.md"
        scenarios_csv = out / f"trade_management_scenarios_{stamp}.csv"
        metrics_csv = out / f"trade_management_metrics_{stamp}.csv"

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        scenarios_df.to_csv(scenarios_csv, index=False)
        metrics_df.to_csv(metrics_csv, index=False)

        ranking = payload.get("ranking", [])
        diagnosis = payload.get("diagnosis", {})
        lines = [
            "# Trade Management Research Lab",
            "",
            f"Generated at: {payload.get('generated_at')}",
            f"Sample size: {payload.get('sample_size')}",
            "",
            "## Ranking",
        ]
        for item in ranking:
            lines.append(f"- {item.get('rank')}. Scenario {item.get('scenario')} (score={item.get('ranking_score'):.4f})")

        lines.extend(
            [
                "",
                "## Diagnosis",
                f"- Decision option: {diagnosis.get('decision_option')}",
                f"- Best scenario: {diagnosis.get('best_scenario')}",
                f"- Recommendation: {diagnosis.get('recommendation')}",
            ]
        )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return {
            "trade_management_json": str(json_path),
            "trade_management_markdown": str(md_path),
            "trade_management_scenarios_csv": str(scenarios_csv),
            "trade_management_metrics_csv": str(metrics_csv),
        }

    def _timeframe_to_minutes(self, timeframe: str) -> int:
        tf = timeframe.strip().lower()
        if tf.endswith("m"):
            return max(1, int(tf[:-1]))
        if tf.endswith("h"):
            return max(1, int(tf[:-1])) * 60
        if tf.endswith("d"):
            return max(1, int(tf[:-1])) * 1440
        return 5
