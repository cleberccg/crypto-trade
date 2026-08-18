"""Edge drift monitor for specialized paper trading operations."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import bindparam, text

from database.connection import get_session


@dataclass(frozen=True)
class EdgeDriftContext:
    symbol: str
    timeframe: str


@dataclass(frozen=True)
class EdgeDriftThresholds:
    attention_health_score: float = 70.0
    critical_health_score: float = 50.0
    attention_metric_degradation_pct: float = 0.15
    critical_metric_degradation_pct: float = 0.30
    attention_drawdown_worsening_pct: float = 0.10
    critical_drawdown_worsening_pct: float = 0.25
    attention_stability_score: float = 70.0
    critical_stability_score: float = 50.0


@dataclass(frozen=True)
class EdgeDriftMonitorConfig:
    strategy_name: str = "ClassicDonchianBreakout"
    strategy_version: str = "v1.0"
    campaign_id: str | None = None
    specialized_report_file: str | None = None
    contexts: tuple[EdgeDriftContext, ...] = ()
    contexts_from_latest_report: bool = True
    lookback_days: int = 7
    history_window: int = 30
    min_validation_days: int = 30
    min_validation_trades: int = 100
    initial_capital: float = 10_000.0
    thresholds: EdgeDriftThresholds = EdgeDriftThresholds()
    output_prefix: str = "edge_drift_monitor"


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


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_json(results_dir: Path, prefix: str) -> Path | None:
    files = sorted(results_dir.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def _latest_any(results_dir: Path, patterns: tuple[str, ...]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(results_dir.glob(pattern), key=lambda path: path.stat().st_mtime))
    return candidates[-1] if candidates else None


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, (dict, list)) else None


def _ratio_score(expected: float | None, observed: float | None, higher_is_better: bool) -> float:
    if expected is None or observed is None:
        return 0.0
    expected_value = float(expected)
    observed_value = float(observed)
    if higher_is_better:
        if expected_value <= 0.0:
            return 100.0 if observed_value > 0.0 else 0.0
        return max(0.0, min(100.0, (observed_value / expected_value) * 100.0))
    if expected_value <= 0.0:
        return 100.0 if observed_value <= 0.0 else 0.0
    if observed_value <= 0.0:
        return 100.0
    return max(0.0, min(100.0, (expected_value / observed_value) * 100.0))


def _alert_level(
    comparisons: dict[str, Any],
    health_score: float | None,
    stability_score: float,
    thresholds: EdgeDriftThresholds,
) -> str:
    if health_score is None:
        return "INSUFFICIENT_REFERENCE"
    if health_score < thresholds.critical_health_score or stability_score < thresholds.critical_stability_score:
        return "CRITICO"
    if health_score < thresholds.attention_health_score or stability_score < thresholds.attention_stability_score:
        return "ATENCAO"

    for metric_name, metric in comparisons.items():
        if not isinstance(metric, dict):
            continue
        delta_pct = metric.get("delta_pct")
        if delta_pct is None:
            continue
        if metric_name in {"drawdown", "mae"}:
            if delta_pct >= thresholds.critical_drawdown_worsening_pct:
                return "CRITICO"
            if delta_pct >= thresholds.attention_drawdown_worsening_pct:
                return "ATENCAO"
        else:
            if delta_pct <= -thresholds.critical_metric_degradation_pct:
                return "CRITICO"
            if delta_pct <= -thresholds.attention_metric_degradation_pct:
                return "ATENCAO"

    return "NORMAL"


def _equity_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0.0:
            drawdown = (peak - value) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    return max_drawdown


def _history_stability(history: list[dict[str, Any]]) -> dict[str, float]:
    if len(history) < 3:
        return {
            "health_score_mean": 100.0,
            "health_score_std": 0.0,
            "stability_score": 100.0,
            "trend_slope": 0.0,
        }

    scores = [float(row.get("health_score") or 0.0) for row in history if row.get("health_score") is not None]
    if len(scores) < 3:
        return {
            "health_score_mean": 100.0,
            "health_score_std": 0.0,
            "stability_score": 100.0,
            "trend_slope": 0.0,
        }

    tail = scores[-min(len(scores), 7) :]
    score_mean = mean(tail)
    score_std = pstdev(tail) if len(tail) > 1 else 0.0
    stability_score = max(0.0, min(100.0, 100.0 * (1.0 - (score_std / (abs(score_mean) + 1e-6)))))
    slope = 0.0
    if len(tail) > 1:
        x_values = list(range(len(tail)))
        x_mean = mean(x_values)
        y_mean = score_mean
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, tail))
        denominator = sum((x - x_mean) ** 2 for x in x_values) or 1.0
        slope = numerator / denominator

    return {
        "health_score_mean": round(score_mean, 6),
        "health_score_std": round(score_std, 6),
        "stability_score": round(stability_score, 6),
        "trend_slope": round(slope, 6),
    }


class EdgeDriftMonitorService:
    """Continuously compares live paper trading against backtest and rolling OOS references."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._campaign_registry_path = self._results_dir / "paper_specialized_campaign_registry.json"

    def run(self, cfg: EdgeDriftMonitorConfig) -> dict[str, Any]:
        reference_report = self._load_reference_report(cfg)
        strategy_name = str(reference_report.get("strategy", {}).get("name") or cfg.strategy_name)
        strategy_version = str(reference_report.get("strategy", {}).get("version") or cfg.strategy_version)
        campaign_execution_ids = self._campaign_execution_ids(cfg.campaign_id)

        contexts = list(cfg.contexts)
        if not contexts and cfg.contexts_from_latest_report:
            contexts = self._contexts_from_report(reference_report)
        if not contexts:
            raise RuntimeError("No approved contexts found for edge drift monitoring.")

        reference_metrics = self._reference_metrics(reference_report)
        operations_reference = self._operations_reference(reference_report)

        windows = {
            "daily": self._build_window_report(
                cfg=cfg,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                contexts=contexts,
                reference_metrics=reference_metrics,
                operations_reference=operations_reference,
                window_days=1,
                execution_ids=campaign_execution_ids,
                campaign_scope_only=bool(cfg.campaign_id),
            ),
            "weekly": self._build_window_report(
                cfg=cfg,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                contexts=contexts,
                reference_metrics=reference_metrics,
                operations_reference=operations_reference,
                window_days=7,
                execution_ids=campaign_execution_ids,
                campaign_scope_only=bool(cfg.campaign_id),
            ),
            "consolidated": self._build_window_report(
                cfg=cfg,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                contexts=contexts,
                reference_metrics=reference_metrics,
                operations_reference=operations_reference,
                window_days=max(1, int(cfg.lookback_days)),
                execution_ids=campaign_execution_ids,
                campaign_scope_only=bool(cfg.campaign_id),
            ),
        }

        history = self._load_history()
        stability = _history_stability(history)
        consolidated = windows["consolidated"]
        health_components = self._health_components(consolidated["comparisons"], stability["stability_score"])
        health_score_raw = health_components.get("final_score")
        health_score = float(health_score_raw) if health_score_raw is not None else None
        alert_level = self._alert_level(consolidated["comparisons"], health_score, stability["stability_score"], cfg.thresholds)
        capital_real_ready = self._capital_ready(consolidated, health_score, stability, cfg)
        promotion_answer = self._promotion_answer(consolidated, health_score, stability, cfg)

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase": "EDGE_DRIFT_MONITOR",
            "strategy": {
                "name": strategy_name,
                "version": strategy_version,
                "classification_specialized": "PAPER_APPROVED_SPECIALIZED",
            },
            "scope": {
                "contexts": [asdict(ctx) for ctx in contexts],
                "context_count": len(contexts),
                "lookback_days": int(cfg.lookback_days),
                "campaign_id": cfg.campaign_id,
                "campaign_execution_ids_count": len(campaign_execution_ids),
                "source_report_file": str(reference_report.get("source_report_file") or ""),
            },
            "reference": reference_metrics,
            "windows": windows,
            "health_score": {
                "value": round(health_score, 6) if health_score is not None else None,
                "alert_level": alert_level,
                "stability": stability,
                "components": health_components,
                "thresholds": asdict(cfg.thresholds),
            },
            "operational_costs": consolidated["operational_costs"],
            "readiness": {
                "min_validation_days": int(cfg.min_validation_days),
                "min_validation_trades": int(cfg.min_validation_trades),
                "capital_real_ready": capital_real_ready,
                "paper_approved_specialized": promotion_answer == "SIM",
                "promotion_answer": promotion_answer,
            },
            "alerts": consolidated["alerts"],
            "reports": {
                "daily": windows["daily"]["summary"],
                "weekly": windows["weekly"]["summary"],
                "consolidated": windows["consolidated"]["summary"],
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        history.append(self._build_history_snapshot(report, consolidated))
        self._write_history(history)

        summary = {
            "strategy": strategy_name,
            "version": strategy_version,
            "campaign_id": cfg.campaign_id,
            "alert_level": alert_level,
            "health_score": round(health_score, 6) if health_score is not None else None,
            "capital_real_ready": capital_real_ready,
            "promotion_answer": promotion_answer,
            "trades": consolidated["summary"]["number_of_trades"],
            "outputs": outputs,
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _load_reference_report(self, cfg: EdgeDriftMonitorConfig) -> dict[str, Any]:
        report_path = Path(cfg.specialized_report_file) if cfg.specialized_report_file else _latest_json(self._results_dir, "paper_specialized_validation_")
        if report_path is not None and report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["source_report_file"] = str(report_path)
                return data

        fallback = _latest_any(
            self._results_dir,
            (
                "edge_external_validation_lab_*.json",
                "edge_operational_pipeline_*.json",
                "edge_discovery_lab_*.json",
            ),
        )
        if fallback is None or not fallback.exists():
            raise RuntimeError("No laboratory reference report found for edge drift monitoring.")

        data = json.loads(fallback.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Laboratory reference report is invalid.")
        data["source_report_file"] = str(fallback)
        return data

    def _contexts_from_report(self, report: dict[str, Any]) -> list[EdgeDriftContext]:
        scope = report.get("scope", {}) if isinstance(report.get("scope"), dict) else {}
        contexts = scope.get("contexts", []) if isinstance(scope.get("contexts"), list) else []
        items: list[EdgeDriftContext] = []
        for row in contexts:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            if symbol and timeframe:
                items.append(EdgeDriftContext(symbol=symbol, timeframe=timeframe))
        return items

    def _reference_metrics(self, report: dict[str, Any]) -> dict[str, Any]:
        winning = report.get("final_executive_report", {}) if isinstance(report.get("final_executive_report"), dict) else {}
        winning_strategy = winning.get("winning_strategy", {}) if isinstance(winning.get("winning_strategy"), dict) else {}

        baseline = report.get("baseline", {}) if isinstance(report.get("baseline"), dict) else {}
        backtest = baseline.get("backtest", {}) if isinstance(baseline.get("backtest"), dict) else {}
        rolling = baseline.get("rolling_oos", {}) if isinstance(baseline.get("rolling_oos"), dict) else {}
        observed_paper = report.get("observed_paper", {}) if isinstance(report.get("observed_paper"), dict) else {}

        if not backtest and winning_strategy:
            backtest = {
                "profit_factor": winning_strategy.get("profit_factor") or winning_strategy.get("pf"),
                "sharpe": winning_strategy.get("sharpe"),
                "expectancy": winning_strategy.get("expectancy"),
                "drawdown": winning_strategy.get("drawdown_pct") or winning_strategy.get("drawdown"),
                "win_rate": winning_strategy.get("win_rate"),
            }
        if not rolling and winning_strategy:
            rolling = {
                "profit_factor": winning_strategy.get("profit_factor") or winning_strategy.get("pf"),
                "sharpe": winning_strategy.get("sharpe"),
                "expectancy": winning_strategy.get("expectancy"),
                "drawdown": winning_strategy.get("drawdown_pct") or winning_strategy.get("drawdown"),
                "win_rate": winning_strategy.get("win_rate"),
            }

        if not observed_paper and winning_strategy:
            observed_paper = {
                "profit_factor": winning_strategy.get("profit_factor") or winning_strategy.get("pf"),
                "sharpe": winning_strategy.get("sharpe"),
                "expectancy": winning_strategy.get("expectancy"),
                "drawdown": winning_strategy.get("drawdown_pct") or winning_strategy.get("drawdown"),
                "win_rate": winning_strategy.get("win_rate"),
            }

        return {
            "backtest": backtest,
            "rolling_oos": rolling,
            "specialized_paper": observed_paper,
        }

    def _operations_reference(self, report: dict[str, Any]) -> dict[str, float | None]:
        operations = report.get("operations", []) if isinstance(report.get("operations"), list) else []
        mfe_values = [_safe_float(row.get("mfe")) for row in operations if isinstance(row, dict) and _safe_float(row.get("mfe")) is not None]
        mae_values = [_safe_float(row.get("mae")) for row in operations if isinstance(row, dict) and _safe_float(row.get("mae")) is not None]
        return {
            "mfe": round(mean(mfe_values), 6) if mfe_values else None,
            "mae": round(mean(mae_values), 6) if mae_values else None,
        }

    def _build_window_report(
        self,
        *,
        cfg: EdgeDriftMonitorConfig,
        strategy_name: str,
        strategy_version: str,
        contexts: list[EdgeDriftContext],
        reference_metrics: dict[str, Any],
        operations_reference: dict[str, float | None],
        window_days: int,
        execution_ids: list[str],
        campaign_scope_only: bool,
    ) -> dict[str, Any]:
        strategy_key = f"{strategy_name}@{strategy_version}"
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(1, int(window_days)))

        if campaign_scope_only and not execution_ids:
            rows = []
        else:
            base_sql = """
                SELECT th.symbol, th.timeframe, th.entry_time, th.exit_time, th.side,
                       th.entry_price, th.exit_price, th.quantity, th.pnl, th.pnl_percent,
                       th.duration_minutes, ss.entry_price AS signal_entry_price,
                       ss.timestamp AS signal_timestamp
                FROM trade_history th
                LEFT JOIN signal_snapshots ss
                  ON ss.strategy = th.strategy
                 AND ss.symbol = th.symbol
                 AND ss.timeframe = th.timeframe
                 AND ss.timestamp = th.entry_time
                 AND ss.signal = 'BUY'
                WHERE th.strategy = :strategy_key
                  AND th.exit_time IS NOT NULL
                  AND th.exit_time >= :start_dt
                  AND th.exit_time < :end_dt
            """
            params: dict[str, Any] = {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt}
            if execution_ids:
                base_sql += "\n                  AND th.execution_id IN :execution_ids\n"
            base_sql += "\n                ORDER BY th.exit_time ASC\n            "
            stmt = text(base_sql)
            if execution_ids:
                stmt = stmt.bindparams(bindparam("execution_ids", expanding=True))
                params["execution_ids"] = execution_ids
            with get_session() as session:
                rows = session.execute(stmt, params).mappings().all()

        trade_rows = [dict(row) for row in rows]
        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}
        scoped = [row for row in trade_rows if (str(row.get("symbol")), str(row.get("timeframe"))) in context_set]

        summary = self._summarize_trades(scoped, cfg.initial_capital)
        comparisons = self._build_comparisons(summary, reference_metrics, cfg, operations_reference)
        health_components = self._health_components(comparisons, 100.0)
        alert_breakdown = self._alert_breakdown(comparisons, health_components)
        operational_costs = self._operational_costs(scoped)

        by_context = [self._context_snapshot(ctx, scoped, cfg.initial_capital, reference_metrics, operations_reference) for ctx in contexts]

        return {
            "window_days": int(window_days),
            "window_start": start_dt.isoformat(),
            "window_end": end_dt.isoformat(),
            "summary": summary,
            "comparisons": comparisons,
            "health_components": health_components,
            "operational_costs": operational_costs,
            "alerts": alert_breakdown,
            "contexts": by_context,
        }

    def _campaign_execution_ids(self, campaign_id: str | None) -> list[str]:
        if not campaign_id:
            return []
        data = _load_json(self._campaign_registry_path)
        if not isinstance(data, dict):
            return []
        campaigns = data.get("campaigns") if isinstance(data.get("campaigns"), dict) else {}
        entry = campaigns.get(str(campaign_id)) if isinstance(campaigns.get(str(campaign_id)), dict) else {}
        raw_ids = entry.get("execution_ids") if isinstance(entry.get("execution_ids"), list) else []
        seen: set[str] = set()
        output: list[str] = []
        for value in raw_ids:
            token = str(value or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            output.append(token)
        return output

    def _summarize_trades(self, trades: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
        pnl_values = [_safe_float(row.get("pnl")) or 0.0 for row in trades]
        pnl_pct_values = [_safe_float(row.get("pnl_percent")) or 0.0 for row in trades]
        wins = [value for value in pnl_values if value > 0.0]
        losses = [value for value in pnl_values if value <= 0.0]
        gross_profit = sum(wins)
        gross_loss_abs = abs(sum(losses))
        profit_factor = gross_profit / gross_loss_abs if gross_loss_abs > 0.0 else (999.0 if gross_profit > 0.0 else 0.0)
        trades_count = len(trades)
        expectancy = (sum(pnl_values) / trades_count) if trades_count else 0.0
        win_rate = (len(wins) / trades_count) if trades_count else 0.0

        sharpe = 0.0
        if len(pnl_pct_values) > 1:
            mu = mean(pnl_pct_values)
            variance = sum((value - mu) ** 2 for value in pnl_pct_values) / (len(pnl_pct_values) - 1)
            std_dev = variance ** 0.5
            if std_dev > 0.0:
                sharpe = mu / std_dev

        equity_curve = []
        cumulative = 0.0
        for pnl in pnl_values:
            cumulative += pnl
            equity_curve.append(initial_capital + cumulative)

        drawdown = _equity_drawdown(equity_curve) if equity_curve else 0.0
        net_profit = sum(pnl_values)
        net_return = net_profit / initial_capital if initial_capital > 0.0 else 0.0

        mfe_values, mae_values, limitations = self._mfe_mae_for_trades(trades)

        entry_times = [row.get("entry_time") for row in trades if isinstance(row.get("entry_time"), datetime)]
        exit_times = [row.get("exit_time") for row in trades if isinstance(row.get("exit_time"), datetime)]
        first_trade = min(entry_times) if entry_times else None
        last_trade = max(exit_times) if exit_times else None
        validation_days = 0
        if isinstance(first_trade, datetime) and isinstance(last_trade, datetime):
            validation_days = max(0, (last_trade - first_trade).days + 1)

        return {
            "number_of_trades": trades_count,
            "net_profit": round(net_profit, 6),
            "net_return": round(net_return, 6),
            "profit_factor": round(profit_factor, 6),
            "sharpe": round(sharpe, 6),
            "expectancy": round(expectancy, 6),
            "drawdown": round(drawdown, 6),
            "win_rate": round(win_rate, 6),
            "mfe": round(mean(mfe_values), 6) if mfe_values else None,
            "mae": round(mean(mae_values), 6) if mae_values else None,
            "validation_days": int(validation_days),
            "mfe_mae_limitations": limitations,
            "first_trade_at": first_trade.isoformat() if isinstance(first_trade, datetime) else None,
            "last_trade_at": last_trade.isoformat() if isinstance(last_trade, datetime) else None,
        }

    def _mfe_mae_for_trades(self, trades: list[dict[str, Any]]) -> tuple[list[float], list[float], dict[str, Any]]:
        mfe_values: list[float] = []
        mae_values: list[float] = []
        limitations = {
            "missing_candles": 0,
            "missing_entry_exit": 0,
        }

        for row in trades:
            entry_time = row.get("entry_time")
            exit_time = row.get("exit_time")
            entry_price = _safe_float(row.get("entry_price")) or 0.0
            if not isinstance(entry_time, datetime) or not isinstance(exit_time, datetime) or entry_price <= 0.0:
                limitations["missing_entry_exit"] += 1
                continue

            symbol = str(row.get("symbol") or "")
            timeframe = str(row.get("timeframe") or "")
            with get_session() as session:
                candle_row = session.execute(
                    text(
                        """
                        SELECT MAX(high) AS max_high, MIN(low) AS min_low
                        FROM candles
                        WHERE symbol = :symbol
                          AND timeframe = :timeframe
                          AND open_time >= :entry_time
                          AND open_time <= :exit_time
                        """
                    ),
                    {"symbol": symbol, "timeframe": timeframe, "entry_time": entry_time, "exit_time": exit_time},
                ).mappings().first()

            if not candle_row or candle_row.get("max_high") is None or candle_row.get("min_low") is None:
                limitations["missing_candles"] += 1
                continue

            max_high = float(candle_row.get("max_high"))
            min_low = float(candle_row.get("min_low"))
            mfe_values.append((max_high - entry_price) / entry_price)
            mae_values.append((min_low - entry_price) / entry_price)

        return mfe_values, mae_values, limitations

    def _build_comparisons(
        self,
        summary: dict[str, Any],
        reference_metrics: dict[str, Any],
        cfg: EdgeDriftMonitorConfig,
        operations_reference: dict[str, float | None],
    ) -> dict[str, Any]:
        baseline = reference_metrics.get("rolling_oos", {}) if isinstance(reference_metrics.get("rolling_oos"), dict) else {}
        if not baseline:
            baseline = reference_metrics.get("backtest", {}) if isinstance(reference_metrics.get("backtest"), dict) else {}

        expected_profit_factor = _safe_float(baseline.get("profit_factor"))
        expected_sharpe = _safe_float(baseline.get("sharpe"))
        expected_expectancy = _safe_float(baseline.get("expectancy"))
        expected_drawdown = _safe_float(baseline.get("drawdown"))
        expected_win_rate = _safe_float(baseline.get("win_rate"))
        expected_net_profit = None
        if expected_expectancy is not None:
            expected_net_profit = float(expected_expectancy) * float(summary.get("number_of_trades") or 0.0)
        expected_net_return = (expected_net_profit / float(cfg.initial_capital)) if (expected_net_profit is not None and cfg.initial_capital > 0.0) else None

        expected_mfe = operations_reference.get("mfe")
        expected_mae = operations_reference.get("mae")

        metrics = {
            "profit_factor": {"expected": expected_profit_factor, "observed": summary.get("profit_factor"), "higher_is_better": True},
            "sharpe": {"expected": expected_sharpe, "observed": summary.get("sharpe"), "higher_is_better": True},
            "expectancy": {"expected": expected_expectancy, "observed": summary.get("expectancy"), "higher_is_better": True},
            "drawdown": {"expected": expected_drawdown, "observed": summary.get("drawdown"), "higher_is_better": False},
            "win_rate": {"expected": expected_win_rate, "observed": summary.get("win_rate"), "higher_is_better": True},
            "net_return": {"expected": expected_net_return, "observed": summary.get("net_return"), "higher_is_better": True},
            "net_profit": {"expected": expected_net_profit, "observed": summary.get("net_profit"), "higher_is_better": True},
            "mfe": {"expected": expected_mfe, "observed": summary.get("mfe"), "higher_is_better": True},
            "mae": {"expected": expected_mae, "observed": summary.get("mae"), "higher_is_better": False},
        }

        for payload in metrics.values():
            expected = payload["expected"]
            observed = payload["observed"]
            payload["delta"] = None if expected is None or observed is None else round(float(observed) - float(expected), 6)
            payload["delta_pct"] = None
            if expected not in (None, 0.0) and observed is not None:
                payload["delta_pct"] = round(((float(observed) - float(expected)) / abs(float(expected))) if abs(float(expected)) > 1e-9 else 0.0, 6)

        return metrics

    def _health_components(self, comparisons: dict[str, Any], stability_score: float) -> dict[str, Any]:
        weights = {
            "profit_factor": 0.22,
            "sharpe": 0.18,
            "expectancy": 0.18,
            "drawdown": 0.15,
            "win_rate": 0.10,
            "net_return": 0.07,
            "net_profit": 0.05,
            "mfe": 0.03,
            "mae": 0.02,
        }

        total = 0.0
        available_weight = 0.0
        available_metrics = 0
        for metric_name, weight in weights.items():
            metric = comparisons.get(metric_name, {}) if isinstance(comparisons.get(metric_name), dict) else {}
            if metric.get("expected") is None or metric.get("observed") is None:
                continue
            score = _ratio_score(metric.get("expected"), metric.get("observed"), bool(metric.get("higher_is_better", True)))
            total += score * weight
            available_weight += weight
            available_metrics += 1

        if available_weight <= 0.0:
            return {
                "base_score": None,
                "stability_score": round(float(stability_score), 6),
                "final_score": None,
                "available_metrics": 0,
                "total_metrics": len(weights),
                "coverage_pct": 0.0,
            }

        base_score = total / available_weight
        final_score = (base_score * 0.9) + (float(stability_score) * 0.1)
        return {
            "base_score": round(base_score, 6),
            "stability_score": round(float(stability_score), 6),
            "final_score": round(max(0.0, min(100.0, final_score)), 6),
            "available_metrics": int(available_metrics),
            "total_metrics": len(weights),
            "coverage_pct": round((available_metrics / len(weights)) * 100.0, 6),
        }

    def _alert_breakdown(self, comparisons: dict[str, Any], health_components: dict[str, float]) -> dict[str, Any]:
        if health_components.get("final_score") is None:
            return {
                "level": "INSUFFICIENT_REFERENCE",
                "critical_reasons": [],
                "attention_reasons": [],
            }

        critical: list[str] = []
        attention: list[str] = []
        for metric_name, metric in comparisons.items():
            if not isinstance(metric, dict):
                continue
            delta_pct = metric.get("delta_pct")
            if delta_pct is None:
                continue
            if metric_name in {"drawdown", "mae"}:
                if delta_pct >= 0.30:
                    critical.append(metric_name)
                elif delta_pct >= 0.15:
                    attention.append(metric_name)
            else:
                if delta_pct <= -0.30:
                    critical.append(metric_name)
                elif delta_pct <= -0.15:
                    attention.append(metric_name)

        if float(health_components.get("final_score") or 0.0) < 50.0:
            critical.append("health_score")
        elif float(health_components.get("final_score") or 0.0) < 70.0:
            attention.append("health_score")

        level = "NORMAL"
        if critical:
            level = "CRITICO"
        elif attention:
            level = "ATENCAO"

        return {
            "level": level,
            "critical_reasons": sorted(set(critical)),
            "attention_reasons": sorted(set(attention)),
        }

    def _alert_level(
        self,
        comparisons: dict[str, Any],
        health_score: float | None,
        stability_score: float,
        thresholds: EdgeDriftThresholds,
    ) -> str:
        return _alert_level(comparisons, health_score, stability_score, thresholds)

    def _capital_ready(
        self,
        consolidated: dict[str, Any],
        health_score: float | None,
        stability: dict[str, float],
        cfg: EdgeDriftMonitorConfig,
    ) -> bool:
        if health_score is None:
            return False
        summary = consolidated["summary"]
        comparisons = consolidated["comparisons"]
        alerts = consolidated["alerts"]
        enough_sample = int(summary.get("validation_days") or 0) >= int(cfg.min_validation_days) or int(summary.get("number_of_trades") or 0) >= int(cfg.min_validation_trades)
        no_critical = not bool(alerts.get("critical_reasons"))
        stability_ok = float(stability.get("stability_score") or 0.0) >= cfg.thresholds.attention_stability_score
        metric_limits_ok = True
        for name in ("profit_factor", "sharpe", "expectancy", "win_rate", "net_return", "net_profit"):
            metric = comparisons.get(name, {}) if isinstance(comparisons.get(name), dict) else {}
            if metric.get("delta_pct") is not None and float(metric.get("delta_pct")) <= -cfg.thresholds.critical_metric_degradation_pct:
                metric_limits_ok = False
        drawdown = comparisons.get("drawdown", {}) if isinstance(comparisons.get("drawdown"), dict) else {}
        if drawdown.get("delta_pct") is not None and float(drawdown.get("delta_pct")) >= cfg.thresholds.critical_drawdown_worsening_pct:
            metric_limits_ok = False
        return enough_sample and no_critical and stability_ok and metric_limits_ok and health_score >= cfg.thresholds.attention_health_score

    def _promotion_answer(
        self,
        consolidated: dict[str, Any],
        health_score: float | None,
        stability: dict[str, float],
        cfg: EdgeDriftMonitorConfig,
    ) -> str:
        if health_score is None:
            return "INSUFFICIENT_REFERENCE"
        summary = consolidated["summary"]
        alerts = consolidated["alerts"]
        enough_sample = int(summary.get("validation_days") or 0) >= int(cfg.min_validation_days) or int(summary.get("number_of_trades") or 0) >= int(cfg.min_validation_trades)
        no_critical = not bool(alerts.get("critical_reasons"))
        stability_ok = float(stability.get("stability_score") or 0.0) >= cfg.thresholds.attention_stability_score
        if enough_sample and no_critical and stability_ok and health_score >= 80.0:
            return "SIM"
        if enough_sample and health_score >= cfg.thresholds.attention_health_score and not alerts.get("critical_reasons"):
            return "PARCIALMENTE"
        return "NAO"

    def _operational_costs(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        slippage_values: list[float] = []
        latency_values: list[float] = []
        spread_values: list[float] = []
        missing_signal_reference = 0

        for row in trades:
            entry_price = _safe_float(row.get("entry_price"))
            signal_entry_price = _safe_float(row.get("signal_entry_price"))
            entry_time = row.get("entry_time")
            signal_time = _parse_dt(row.get("signal_timestamp"))

            if entry_price is not None and signal_entry_price is not None:
                slippage_values.append(abs(entry_price - signal_entry_price))
            else:
                missing_signal_reference += 1

            if isinstance(entry_time, datetime) and isinstance(signal_time, datetime):
                latency_values.append(max(0.0, (entry_time - signal_time).total_seconds()))

        return {
            "slippage_observed_avg": round(mean(slippage_values), 6) if slippage_values else None,
            "latency_seconds_avg": round(mean(latency_values), 6) if latency_values else None,
            "spread_observed_avg": round(mean(spread_values), 6) if spread_values else None,
            "missing_signal_reference_trades": int(missing_signal_reference),
            "spread_limitations": "spread_not_available_in_current_infrastructure",
        }

    def _context_snapshot(
        self,
        ctx: EdgeDriftContext,
        trades: list[dict[str, Any]],
        initial_capital: float,
        reference_metrics: dict[str, Any],
        operations_reference: dict[str, float | None],
    ) -> dict[str, Any]:
        scoped = [row for row in trades if str(row.get("symbol")) == ctx.symbol and str(row.get("timeframe")) == ctx.timeframe]
        summary = self._summarize_trades(scoped, initial_capital)
        comparisons = self._build_comparisons(
            summary,
            reference_metrics,
            EdgeDriftMonitorConfig(initial_capital=initial_capital),
            operations_reference,
        )
        return {
            "context": asdict(ctx),
            "summary": summary,
            "comparisons": comparisons,
        }

    def _build_history_snapshot(self, report: dict[str, Any], consolidated: dict[str, Any]) -> dict[str, Any]:
        return {
            "generated_at": report.get("generated_at"),
            "strategy": (report.get("strategy") or {}).get("name"),
            "strategy_version": (report.get("strategy") or {}).get("version"),
            "scope": report.get("scope"),
            "alert_level": (report.get("health_score") or {}).get("alert_level"),
            "health_score": (report.get("health_score") or {}).get("value"),
            "trades": consolidated["summary"].get("number_of_trades"),
            "validation_days": consolidated["summary"].get("validation_days"),
            "operational_costs": report.get("operational_costs"),
        }

    def _load_history(self) -> list[dict[str, Any]]:
        path = self._results_dir / "edge_drift_monitor_history.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _write_history(self, history: list[dict[str, Any]]) -> None:
        path = self._results_dir / "edge_drift_monitor_history.json"
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _write_outputs(self, output_prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        return {"json": str(json_path), "md": str(md_path), "history": str(self._results_dir / "edge_drift_monitor_history.json")}

    def _to_markdown(self, report: dict[str, Any]) -> str:
        health = report.get("health_score", {}) if isinstance(report.get("health_score"), dict) else {}
        readiness = report.get("readiness", {}) if isinstance(report.get("readiness"), dict) else {}
        consolidated = report.get("windows", {}).get("consolidated", {}) if isinstance(report.get("windows"), dict) else {}

        lines = [
            "# Edge Drift Monitor",
            "",
            f"- Strategy: {(report.get('strategy') or {}).get('name')}",
            f"- Version: {(report.get('strategy') or {}).get('version')}",
            f"- Health Score: {health.get('value')}",
            f"- Alert Level: {health.get('alert_level')}",
            f"- PAPER_APPROVED_SPECIALIZED: {readiness.get('paper_approved_specialized')}",
            f"- Capital real ready: {readiness.get('capital_real_ready')}",
            "",
            "## Consolidated",
            f"- Trades: {consolidated.get('summary', {}).get('number_of_trades')}",
            f"- Net profit: {consolidated.get('summary', {}).get('net_profit')}",
            f"- Profit factor: {consolidated.get('summary', {}).get('profit_factor')}",
            f"- Sharpe: {consolidated.get('summary', {}).get('sharpe')}",
            f"- Expectancy: {consolidated.get('summary', {}).get('expectancy')}",
            f"- Drawdown: {consolidated.get('summary', {}).get('drawdown')}",
            f"- Win rate: {consolidated.get('summary', {}).get('win_rate')}",
            f"- MFE: {consolidated.get('summary', {}).get('mfe')}",
            f"- MAE: {consolidated.get('summary', {}).get('mae')}",
            "",
            "## Alerts",
            f"- Level: {(report.get('alerts') or {}).get('level')}",
            f"- Critical reasons: {', '.join((report.get('alerts') or {}).get('critical_reasons', []))}",
            f"- Attention reasons: {', '.join((report.get('alerts') or {}).get('attention_reasons', []))}",
            "",
            "## Daily / Weekly / Consolidated",
        ]

        windows = report.get("windows", {}) if isinstance(report.get("windows"), dict) else {}
        for label in ("daily", "weekly", "consolidated"):
            window = windows.get(label, {}) if isinstance(windows.get(label), dict) else {}
            summary = window.get("summary", {}) if isinstance(window.get("summary"), dict) else {}
            lines.extend([
                f"### {label.title()}",
                f"- Window days: {window.get('window_days')}",
                f"- Trades: {summary.get('number_of_trades')}",
                f"- Profit factor: {summary.get('profit_factor')}",
            ])
        return "\n".join(lines) + "\n"
