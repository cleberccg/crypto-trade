"""Specialized paper-trading validation for context-restricted operational edge."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from database.connection import get_session
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService


@dataclass(frozen=True)
class SpecializedContext:
    symbol: str
    timeframe: str


@dataclass(frozen=True)
class ValidationCriteria:
    min_days: int = 30
    min_trades: int = 100
    min_profit_factor: float = 1.15
    min_expectancy: float = 0.0
    min_sharpe: float = 0.0
    max_drawdown: float = 0.20
    max_pf_degradation_pct: float = 0.35
    max_sharpe_degradation_pct: float = 0.40
    max_expectancy_degradation_pct: float = 0.40
    max_win_rate_degradation_pct: float = 0.35
    max_drawdown_worsening_pct: float = 0.60


@dataclass(frozen=True)
class SpecializedBaseline:
    profit_factor: float | None = None
    sharpe: float | None = None
    expectancy: float | None = None
    drawdown: float | None = None
    win_rate: float | None = None


@dataclass(frozen=True)
class SpecializedPaperValidationConfig:
    strategy_name: str = "ClassicDonchianBreakout"
    strategy_version: str = "v1.0"
    run_live: bool = False
    max_global_cycles: int = 0
    poll_seconds: float = 15.0
    bootstrap_bars: int = 1500
    bootstrap_replay_bars: int = 350
    initial_capital: float = 10_000.0
    min_trades_before_change: int = 100
    contexts: tuple[SpecializedContext, ...] = ()
    contexts_from_matrix: bool = True
    context_min_trades: int = 5
    context_min_profit_factor: float = 1.0
    context_min_expectancy: float = 0.0
    hypothesis_config: dict[str, Any] | None = None
    edge_matrix_csv: str | None = None
    backtest_baseline: SpecializedBaseline = SpecializedBaseline(
        profit_factor=2.627682,
        sharpe=0.126418,
        expectancy=2.276779,
        drawdown=0.0079,
        win_rate=None,
    )
    rolling_oos_baseline: SpecializedBaseline = SpecializedBaseline(
        profit_factor=2.627682,
        sharpe=0.126418,
        expectancy=2.276779,
        drawdown=0.0079,
        win_rate=None,
    )
    criteria: ValidationCriteria = ValidationCriteria()
    output_prefix: str = "paper_specialized_validation"


class SpecializedPaperValidationService:
    """Runs specialized context-only paper validation and compares against expected baselines."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: SpecializedPaperValidationConfig) -> dict[str, Any]:
        contexts = list(cfg.contexts)
        if cfg.contexts_from_matrix and not contexts:
            contexts = self._load_winning_contexts_from_matrix(cfg)
        if not contexts:
            raise RuntimeError(
                "No specialized contexts available. Provide --contexts or ensure edge_discovery matrix exists with positive contexts."
            )

        execution_runs: list[dict[str, Any]] = []
        if cfg.run_live:
            execution_runs = self._run_live_cycles(cfg, contexts)

        observed = self._collect_observed_metrics(cfg, contexts)
        operations = self._collect_operations(cfg, contexts)

        comparison = self._build_comparison(cfg, observed)
        deviations = self._build_deviations(cfg, observed, comparison)
        gate = self._evaluate_gate(cfg, observed, deviations, contexts)
        verdict = self._final_verdict(gate)

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase": "PAPER_SPECIALIZED_VALIDATION",
            "strategy": {
                "name": cfg.strategy_name,
                "version": cfg.strategy_version,
                "classification_universal_reference": "PAPER_APPROVED_UNIVERSAL",
                "classification_specialized": "PAPER_APPROVED_SPECIALIZED",
            },
            "scope": {
                "context_policy": "restricted_to_approved_contexts",
                "contexts": [asdict(ctx) for ctx in contexts],
                "context_count": len(contexts),
            },
            "execution": {
                "run_live": bool(cfg.run_live),
                "max_global_cycles": int(cfg.max_global_cycles),
                "runs": execution_runs,
            },
            "baseline": {
                "backtest": asdict(cfg.backtest_baseline),
                "rolling_oos": asdict(cfg.rolling_oos_baseline),
            },
            "observed_paper": observed,
            "comparison_backtest_oos_paper": comparison,
            "deviations": deviations,
            "validation_criteria": asdict(cfg.criteria),
            "validation_gate": gate,
            "final_answer": verdict,
            "operations": operations,
            "reports": self._build_report_index(cfg, observed, comparison, deviations, operations),
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        history = self._update_history(cfg, report)
        ranking = self._build_ranking(history)
        self._write_ranking(cfg.output_prefix, ranking)

        summary = {
            "strategy": cfg.strategy_name,
            "verdict": verdict.get("answer"),
            "classification": gate.get("proposed_status"),
            "contexts": len(contexts),
            "trades": int(observed.get("number_of_trades") or 0),
            "profit_factor": observed.get("profit_factor"),
            "sharpe": observed.get("sharpe"),
            "expectancy": observed.get("expectancy"),
            "drawdown": observed.get("drawdown"),
            "outputs": outputs,
        }
        return {
            "summary": summary,
            "report": report,
            "outputs": outputs,
        }

    def _run_live_cycles(
        self,
        cfg: SpecializedPaperValidationConfig,
        contexts: list[SpecializedContext],
    ) -> list[dict[str, Any]]:
        service = PaperLiveService(self._base_dir)
        max_cycles = int(cfg.max_global_cycles)
        cycle = 0
        runs: list[dict[str, Any]] = []

        while True:
            cycle += 1
            for ctx in contexts:
                run = service.run(
                    PaperLiveConfig(
                        symbol=ctx.symbol,
                        timeframe=ctx.timeframe,
                        strategy_name=cfg.strategy_name,
                        strategy_version=cfg.strategy_version,
                        initial_capital=max(100.0, float(cfg.initial_capital)),
                        poll_seconds=max(1.0, float(cfg.poll_seconds)),
                        bootstrap_bars=max(200, int(cfg.bootstrap_bars)),
                        bootstrap_replay_bars=max(60, int(cfg.bootstrap_replay_bars)),
                        max_cycles=1,
                        resume=True,
                        min_trades_before_change=max(0, int(cfg.min_trades_before_change)),
                        hypothesis_config=cfg.hypothesis_config,
                        output_prefix=f"{cfg.output_prefix}_{self._slug(ctx.symbol)}_{ctx.timeframe}",
                    )
                )
                runs.append(
                    {
                        "global_cycle": cycle,
                        "symbol": ctx.symbol,
                        "timeframe": ctx.timeframe,
                        "status": run.get("status"),
                        "execution_id": run.get("execution_id"),
                        "closed_trades": run.get("closed_trades"),
                        "processed_bars": run.get("processed_bars"),
                    }
                )

            if max_cycles > 0 and cycle >= max_cycles:
                break

        return runs

    def _collect_observed_metrics(
        self,
        cfg: SpecializedPaperValidationConfig,
        contexts: list[SpecializedContext],
    ) -> dict[str, Any]:
        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}
        strategy_key = f"{cfg.strategy_name}@{cfg.strategy_version}"

        trades: list[dict[str, Any]] = []
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT symbol, timeframe, entry_time, exit_time, pnl, pnl_percent, duration_minutes
                    FROM trade_history
                    WHERE strategy = :strategy_key
                      AND exit_time IS NOT NULL
                    ORDER BY exit_time ASC
                    """
                ),
                {"strategy_key": strategy_key},
            ).mappings().all()
            trades = [dict(row) for row in rows]

        scoped = [t for t in trades if (str(t.get("symbol")), str(t.get("timeframe"))) in context_set]
        if not scoped:
            return {
                "number_of_trades": 0,
                "net_return": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "expectancy": 0.0,
                "drawdown": 0.0,
                "win_rate": 0.0,
                "avg_time_in_position_minutes": 0.0,
                "mfe": 0.0,
                "mae": 0.0,
                "validation_days": 0,
                "first_trade_at": None,
                "last_trade_at": None,
                "outside_scope_trades": len(trades),
            }

        pnl = [float(t.get("pnl") or 0.0) for t in scoped]
        pnl_pct = [float(t.get("pnl_percent") or 0.0) for t in scoped]
        durations = [float(t.get("duration_minutes") or 0.0) for t in scoped]

        wins = [v for v in pnl if v > 0.0]
        losses = [v for v in pnl if v <= 0.0]
        gross_profit = sum(wins)
        gross_loss_abs = abs(sum(losses))
        pf = gross_profit / gross_loss_abs if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)
        net = sum(pnl)
        trades_n = len(scoped)
        expectancy = net / trades_n if trades_n else 0.0
        win_rate = (len(wins) / trades_n) if trades_n else 0.0

        sharpe = 0.0
        if len(pnl_pct) > 1:
            mean_ret = sum(pnl_pct) / len(pnl_pct)
            variance = sum((x - mean_ret) ** 2 for x in pnl_pct) / (len(pnl_pct) - 1)
            std = variance ** 0.5
            if std > 0:
                sharpe = mean_ret / std

        drawdown = self._max_drawdown_from_returns(pnl_pct)
        mfe, mae = self._compute_mfe_mae(cfg, scoped)

        first_trade = scoped[0].get("entry_time")
        last_trade = scoped[-1].get("exit_time")
        validation_days = 0
        if isinstance(first_trade, datetime) and isinstance(last_trade, datetime):
            validation_days = max(0, (last_trade - first_trade).days + 1)

        outside_scope = len([t for t in trades if (str(t.get("symbol")), str(t.get("timeframe"))) not in context_set])

        return {
            "number_of_trades": trades_n,
            "net_return": round(net, 6),
            "profit_factor": round(pf, 6),
            "sharpe": round(sharpe, 6),
            "expectancy": round(expectancy, 6),
            "drawdown": round(drawdown, 6),
            "win_rate": round(win_rate, 6),
            "avg_time_in_position_minutes": round((sum(durations) / len(durations)) if durations else 0.0, 6),
            "mfe": round(mfe, 6),
            "mae": round(mae, 6),
            "validation_days": int(validation_days),
            "first_trade_at": first_trade.isoformat() if isinstance(first_trade, datetime) else None,
            "last_trade_at": last_trade.isoformat() if isinstance(last_trade, datetime) else None,
            "outside_scope_trades": int(outside_scope),
        }

    def _collect_operations(
        self,
        cfg: SpecializedPaperValidationConfig,
        contexts: list[SpecializedContext],
    ) -> list[dict[str, Any]]:
        strategy_key = f"{cfg.strategy_name}@{cfg.strategy_version}"
        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}

        rows: list[dict[str, Any]] = []
        with get_session() as session:
            result = session.execute(
                text(
                    """
                    SELECT th.symbol, th.timeframe, th.entry_time, th.exit_time, th.side,
                           th.entry_price, th.exit_price, th.stop_loss, th.take_profit,
                           th.pnl, th.duration_minutes, th.exit_reason, th.quantity,
                           ss.entry_price AS signal_entry_price, ss.market_regime,
                           ss.score AS signal_score, ss.rr AS signal_rr
                    FROM trade_history th
                    LEFT JOIN signal_snapshots ss
                      ON ss.strategy = th.strategy
                     AND ss.symbol = th.symbol
                     AND ss.timeframe = th.timeframe
                     AND ss.timestamp = th.entry_time
                     AND ss.signal = 'BUY'
                    WHERE th.strategy = :strategy_key
                      AND th.exit_time IS NOT NULL
                    ORDER BY th.exit_time ASC
                    """
                ),
                {"strategy_key": strategy_key},
            ).mappings().all()
            rows = [dict(row) for row in result]

        operations: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol"))
            timeframe = str(row.get("timeframe"))
            in_scope = (symbol, timeframe) in context_set
            signal_entry = row.get("signal_entry_price")
            entry_price = float(row.get("entry_price") or 0.0)
            slippage = abs(entry_price - float(signal_entry)) if signal_entry is not None else None
            mfe, mae = self._trade_mfe_mae(
                symbol=symbol,
                timeframe=timeframe,
                entry_time=row.get("entry_time"),
                exit_time=row.get("exit_time"),
                entry_price=entry_price,
                quantity=float(row.get("quantity") or 0.0),
            )

            operations.append(
                {
                    "timestamp": row.get("entry_time").isoformat() if isinstance(row.get("entry_time"), datetime) else str(row.get("entry_time")),
                    "asset": symbol,
                    "timeframe": timeframe,
                    "direction": row.get("side"),
                    "entry_price": row.get("entry_price"),
                    "exit_price": row.get("exit_price"),
                    "stop": row.get("stop_loss"),
                    "target": row.get("take_profit"),
                    "profit_loss": row.get("pnl"),
                    "duration_minutes": row.get("duration_minutes"),
                    "slippage_observed": slippage,
                    "spread": None,
                    "regime_identified": row.get("market_regime"),
                    "operational_context": f"{symbol}|{timeframe}",
                    "in_specialized_scope": bool(in_scope),
                    "entry_reason": self._entry_reason(row),
                    "exit_reason": row.get("exit_reason"),
                    "mfe": mfe,
                    "mae": mae,
                }
            )
        return operations

    def _build_comparison(
        self,
        cfg: SpecializedPaperValidationConfig,
        observed: dict[str, Any],
    ) -> dict[str, Any]:
        expected = {
            "profit_factor": cfg.rolling_oos_baseline.profit_factor if cfg.rolling_oos_baseline.profit_factor is not None else cfg.backtest_baseline.profit_factor,
            "sharpe": cfg.rolling_oos_baseline.sharpe if cfg.rolling_oos_baseline.sharpe is not None else cfg.backtest_baseline.sharpe,
            "expectancy": cfg.rolling_oos_baseline.expectancy if cfg.rolling_oos_baseline.expectancy is not None else cfg.backtest_baseline.expectancy,
            "drawdown": cfg.rolling_oos_baseline.drawdown if cfg.rolling_oos_baseline.drawdown is not None else cfg.backtest_baseline.drawdown,
            "win_rate": cfg.rolling_oos_baseline.win_rate if cfg.rolling_oos_baseline.win_rate is not None else cfg.backtest_baseline.win_rate,
        }
        return {
            "backtest": asdict(cfg.backtest_baseline),
            "rolling_oos": asdict(cfg.rolling_oos_baseline),
            "expected_reference": expected,
            "paper_observed": {
                "profit_factor": observed.get("profit_factor"),
                "sharpe": observed.get("sharpe"),
                "expectancy": observed.get("expectancy"),
                "drawdown": observed.get("drawdown"),
                "win_rate": observed.get("win_rate"),
            },
        }

    def _build_deviations(
        self,
        cfg: SpecializedPaperValidationConfig,
        observed: dict[str, Any],
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        expected = comparison.get("expected_reference", {}) if isinstance(comparison.get("expected_reference"), dict) else {}

        deviations: dict[str, dict[str, Any]] = {}
        for metric in ("profit_factor", "sharpe", "expectancy", "drawdown", "win_rate"):
            exp = expected.get(metric)
            obs = observed.get(metric)
            if exp is None or obs is None:
                deviations[metric] = {
                    "expected": exp,
                    "observed": obs,
                    "delta": None,
                    "delta_pct": None,
                    "degraded": False,
                }
                continue

            exp_f = float(exp)
            obs_f = float(obs)
            delta = obs_f - exp_f
            delta_pct = 0.0 if abs(exp_f) < 1e-9 else (delta / abs(exp_f))

            degraded = False
            if metric == "profit_factor":
                degraded = delta_pct < -float(cfg.criteria.max_pf_degradation_pct)
            elif metric == "sharpe":
                degraded = delta_pct < -float(cfg.criteria.max_sharpe_degradation_pct)
            elif metric == "expectancy":
                degraded = delta_pct < -float(cfg.criteria.max_expectancy_degradation_pct)
            elif metric == "win_rate":
                degraded = delta_pct < -float(cfg.criteria.max_win_rate_degradation_pct)
            elif metric == "drawdown":
                degraded = delta_pct > float(cfg.criteria.max_drawdown_worsening_pct)

            deviations[metric] = {
                "expected": round(exp_f, 6),
                "observed": round(obs_f, 6),
                "delta": round(delta, 6),
                "delta_pct": round(delta_pct, 6),
                "degraded": bool(degraded),
            }

        relevant = [name for name, item in deviations.items() if bool(item.get("degraded"))]
        return {
            "metrics": deviations,
            "relevant_degradations": relevant,
            "relevant_degradation_count": len(relevant),
        }

    def _evaluate_gate(
        self,
        cfg: SpecializedPaperValidationConfig,
        observed: dict[str, Any],
        deviations: dict[str, Any],
        contexts: list[SpecializedContext],
    ) -> dict[str, Any]:
        criteria = cfg.criteria

        trades = int(observed.get("number_of_trades") or 0)
        days = int(observed.get("validation_days") or 0)
        pf = float(observed.get("profit_factor") or 0.0)
        sharpe = float(observed.get("sharpe") or 0.0)
        expectancy = float(observed.get("expectancy") or 0.0)
        drawdown = float(observed.get("drawdown") or 0.0)
        outside_scope = int(observed.get("outside_scope_trades") or 0)

        enough_sample = days >= int(criteria.min_days) or trades >= int(criteria.min_trades)
        metrics_ok = (
            pf >= float(criteria.min_profit_factor)
            and expectancy > float(criteria.min_expectancy)
            and sharpe > float(criteria.min_sharpe)
            and drawdown <= float(criteria.max_drawdown)
        )
        no_relevant_degradation = int(deviations.get("relevant_degradation_count") or 0) == 0
        scope_respected = outside_scope == 0 and len(contexts) > 0

        all_criteria = enough_sample and metrics_ok and no_relevant_degradation and scope_respected

        failures: list[str] = []
        if not enough_sample:
            failures.append("minimum_validation_window_not_met")
        if not metrics_ok:
            failures.append("quantitative_metrics_not_met")
        if not no_relevant_degradation:
            failures.append("relevant_degradation_detected")
        if not scope_respected:
            failures.append("operation_outside_approved_contexts")

        proposed_status = "PAPER_APPROVED_SPECIALIZED" if all_criteria else "PAPER_CANDIDATE"

        return {
            "enough_sample": enough_sample,
            "metrics_ok": metrics_ok,
            "no_relevant_degradation": no_relevant_degradation,
            "scope_respected": scope_respected,
            "all_criteria_met": all_criteria,
            "proposed_status": proposed_status,
            "failed_reasons": failures,
            "paper_approved_universal_eligible": False,
        }

    def _final_verdict(self, gate: dict[str, Any]) -> dict[str, Any]:
        all_criteria = bool(gate.get("all_criteria_met"))
        metrics_ok = bool(gate.get("metrics_ok"))
        sample_ok = bool(gate.get("enough_sample"))

        if all_criteria:
            answer = "SIM"
            recommendation = (
                "Promover para PAPER_APPROVED_SPECIALIZED e seguir para operacao com pequeno capital real, "
                "restrita aos contextos aprovados, com monitoramento continuo e risco conservador."
            )
        elif metrics_ok and sample_ok:
            answer = "PARCIALMENTE"
            recommendation = "Manter PAPER_CANDIDATE e prolongar paper trading especializado ate fechar todos os criterios."
        else:
            answer = "NAO"
            recommendation = (
                "Manter PAPER_CANDIDATE, registrar motivos de divergencia e reabrir investigacao apenas apos fechamento desta fase."
            )

        return {
            "question": "A estrategia reproduziu em paper trading o edge observado no backtest e rolling OOS?",
            "answer": answer,
            "recommended_next_step": recommendation,
        }

    def _build_report_index(
        self,
        cfg: SpecializedPaperValidationConfig,
        observed: dict[str, Any],
        comparison: dict[str, Any],
        deviations: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        daily = self._aggregate_window(operations, now - timedelta(days=1))
        weekly = self._aggregate_window(operations, now - timedelta(days=7))
        consolidated = {
            "observed_metrics": observed,
            "comparison": comparison,
            "deviations": deviations,
        }
        return {
            "daily_report": daily,
            "weekly_report": weekly,
            "consolidated_report": consolidated,
            "backtest_oos_paper_comparison": comparison,
            "metrics_evolution": self._metrics_evolution_stub(observed),
            "operational_deviations": deviations,
            "historical_ranking_note": "ranking persisted in paper_specialized_history.json",
        }

    def _aggregate_window(self, operations: list[dict[str, Any]], threshold: datetime) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        for op in operations:
            raw = op.get("timestamp")
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
            if isinstance(ts, datetime) and ts >= threshold:
                selected.append(op)

        pnl = [float(op.get("profit_loss") or 0.0) for op in selected]
        wins = [x for x in pnl if x > 0.0]
        losses = [x for x in pnl if x <= 0.0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        return {
            "trades": len(selected),
            "net_return": round(sum(pnl), 6),
            "profit_factor": round(pf, 6),
            "win_rate": round((len(wins) / len(selected)) if selected else 0.0, 6),
        }

    def _metrics_evolution_stub(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
        now = datetime.now(tz=timezone.utc).isoformat()
        return [
            {
                "timestamp": now,
                "profit_factor": observed.get("profit_factor"),
                "sharpe": observed.get("sharpe"),
                "expectancy": observed.get("expectancy"),
                "drawdown": observed.get("drawdown"),
                "win_rate": observed.get("win_rate"),
                "number_of_trades": observed.get("number_of_trades"),
            }
        ]

    def _write_outputs(self, output_prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        ops_csv = self._results_dir / f"{output_prefix}_{stamp}_operations.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        self._write_operations_csv(ops_csv, report.get("operations", []))

        return {
            "json": str(json_path),
            "md": str(md_path),
            "operations_csv": str(ops_csv),
        }

    def _to_markdown(self, report: dict[str, Any]) -> str:
        observed = report.get("observed_paper", {}) if isinstance(report.get("observed_paper"), dict) else {}
        gate = report.get("validation_gate", {}) if isinstance(report.get("validation_gate"), dict) else {}
        verdict = report.get("final_answer", {}) if isinstance(report.get("final_answer"), dict) else {}
        lines = [
            "# Paper Specialized Validation",
            "",
            f"- Strategy: {(report.get('strategy') or {}).get('name')}",
            f"- Status proposal: {gate.get('proposed_status')}",
            f"- Final answer: {verdict.get('answer')}",
            "",
            "## Observed Metrics",
            f"- Profit Factor: {observed.get('profit_factor')}",
            f"- Sharpe: {observed.get('sharpe')}",
            f"- Expectancy: {observed.get('expectancy')}",
            f"- Drawdown: {observed.get('drawdown')}",
            f"- Win Rate: {observed.get('win_rate')}",
            f"- Trades: {observed.get('number_of_trades')}",
            "",
            "## Validation Gate",
            f"- Enough sample: {gate.get('enough_sample')}",
            f"- Metrics OK: {gate.get('metrics_ok')}",
            f"- No relevant degradation: {gate.get('no_relevant_degradation')}",
            f"- Scope respected: {gate.get('scope_respected')}",
            f"- Failed reasons: {', '.join(gate.get('failed_reasons', [])) if isinstance(gate.get('failed_reasons'), list) else ''}",
            "",
            "## Recommendation",
            f"- {verdict.get('recommended_next_step')}",
        ]
        return "\n".join(lines) + "\n"

    def _write_operations_csv(self, path: Path, operations: list[dict[str, Any]]) -> None:
        if not operations:
            path.write_text("", encoding="utf-8")
            return
        fieldnames = sorted({k for op in operations for k in op.keys()})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for op in operations:
                writer.writerow(op)

    def _update_history(self, cfg: SpecializedPaperValidationConfig, report: dict[str, Any]) -> list[dict[str, Any]]:
        path = self._results_dir / "paper_specialized_history.json"
        if path.exists():
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
                history = content if isinstance(content, list) else []
            except json.JSONDecodeError:
                history = []
        else:
            history = []

        observed = report.get("observed_paper", {}) if isinstance(report.get("observed_paper"), dict) else {}
        gate = report.get("validation_gate", {}) if isinstance(report.get("validation_gate"), dict) else {}
        verdict = report.get("final_answer", {}) if isinstance(report.get("final_answer"), dict) else {}

        history.append(
            {
                "generated_at": report.get("generated_at"),
                "strategy": cfg.strategy_name,
                "strategy_version": cfg.strategy_version,
                "status": gate.get("proposed_status"),
                "answer": verdict.get("answer"),
                "profit_factor": observed.get("profit_factor"),
                "sharpe": observed.get("sharpe"),
                "expectancy": observed.get("expectancy"),
                "drawdown": observed.get("drawdown"),
                "win_rate": observed.get("win_rate"),
                "number_of_trades": observed.get("number_of_trades"),
            }
        )

        path.write_text(json.dumps(history, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return history

    def _build_ranking(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for row in history:
            pf = float(row.get("profit_factor") or 0.0)
            sharpe = float(row.get("sharpe") or 0.0)
            dd = float(row.get("drawdown") or 0.0)
            score = (pf * 40.0) + (sharpe * 25.0) + (max(0.0, 1.0 - dd) * 35.0)
            item = dict(row)
            item["ranking_score"] = round(score, 6)
            ranked.append(item)

        ranked.sort(key=lambda x: float(x.get("ranking_score") or 0.0), reverse=True)
        for idx, row in enumerate(ranked, start=1):
            row["ranking"] = idx
        return ranked

    def _write_ranking(self, output_prefix: str, ranking: list[dict[str, Any]]) -> None:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self._results_dir / f"{output_prefix}_{stamp}_ranking_history.csv"
        if not ranking:
            path.write_text("", encoding="utf-8")
            return

        fields = sorted({k for row in ranking for k in row.keys()})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in ranking:
                writer.writerow(row)

    def _load_winning_contexts_from_matrix(self, cfg: SpecializedPaperValidationConfig) -> list[SpecializedContext]:
        matrix_path = Path(cfg.edge_matrix_csv) if cfg.edge_matrix_csv else self._latest_matrix_file()
        if matrix_path is None or not matrix_path.exists():
            return []

        contexts: list[SpecializedContext] = []
        seen: set[tuple[str, str]] = set()

        with matrix_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [row for row in reader if isinstance(row, dict)]

        candidates = [
            row
            for row in rows
            if str(row.get("strategy") or "") == cfg.strategy_name
            and str(row.get("platform_strategy_name") or "") == cfg.strategy_name
            and str(row.get("regime_type") or "") == "full"
            and self._float(row.get("profit_factor"), 0.0) >= float(cfg.context_min_profit_factor)
            and self._float(row.get("expectancy"), -999.0) > float(cfg.context_min_expectancy)
            and self._int(row.get("number_of_trades"), 0) >= int(cfg.context_min_trades)
        ]

        candidates.sort(
            key=lambda r: (
                -self._float(r.get("context_score"), 0.0),
                -self._float(r.get("profit_factor"), 0.0),
                -self._int(r.get("number_of_trades"), 0),
            )
        )

        for row in candidates:
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            key = (symbol, timeframe)
            if not symbol or not timeframe or key in seen:
                continue
            seen.add(key)
            contexts.append(SpecializedContext(symbol=symbol, timeframe=timeframe))

        return contexts

    def _latest_matrix_file(self) -> Path | None:
        files = sorted(self._results_dir.glob("edge_discovery_lab_*_matrix.csv"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    def _compute_mfe_mae(self, cfg: SpecializedPaperValidationConfig, trades: list[dict[str, Any]]) -> tuple[float, float]:
        mfe_values: list[float] = []
        mae_values: list[float] = []
        for row in trades:
            mfe, mae = self._trade_mfe_mae(
                symbol=str(row.get("symbol")),
                timeframe=str(row.get("timeframe")),
                entry_time=row.get("entry_time"),
                exit_time=row.get("exit_time"),
                entry_price=float(row.get("entry_price") or 0.0),
                quantity=1.0,
            )
            mfe_values.append(float(mfe or 0.0))
            mae_values.append(float(mae or 0.0))

        mfe_avg = (sum(mfe_values) / len(mfe_values)) if mfe_values else 0.0
        mae_avg = (sum(mae_values) / len(mae_values)) if mae_values else 0.0
        return mfe_avg, mae_avg

    def _trade_mfe_mae(
        self,
        *,
        symbol: str,
        timeframe: str,
        entry_time: Any,
        exit_time: Any,
        entry_price: float,
        quantity: float,
    ) -> tuple[float | None, float | None]:
        if not isinstance(entry_time, datetime) or not isinstance(exit_time, datetime):
            return None, None
        if entry_price <= 0:
            return None, None

        with get_session() as session:
            row = session.execute(
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
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                },
            ).mappings().first()

        if not row:
            return None, None

        max_high = row.get("max_high")
        min_low = row.get("min_low")
        if max_high is None or min_low is None:
            return None, None

        mfe = ((float(max_high) - entry_price) / entry_price) * max(1.0, float(quantity))
        mae = ((float(min_low) - entry_price) / entry_price) * max(1.0, float(quantity))
        return float(mfe), float(mae)

    def _entry_reason(self, row: dict[str, Any]) -> str:
        parts: list[str] = []
        if row.get("signal_score") is not None:
            parts.append(f"score={row.get('signal_score')}")
        if row.get("signal_rr") is not None:
            parts.append(f"rr={row.get('signal_rr')}")
        if row.get("market_regime") is not None:
            parts.append(f"regime={row.get('market_regime')}")
        return "strategy_entry" if not parts else "strategy_entry(" + ", ".join(parts) + ")"

    def _max_drawdown_from_returns(self, returns: list[float]) -> float:
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in returns:
            equity *= (1.0 + float(r))
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _slug(self, value: str) -> str:
        return value.replace("/", "_").replace(" ", "_")

    def _float(self, value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if parsed != parsed:
            return float(default)
        return parsed

    def _int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)
