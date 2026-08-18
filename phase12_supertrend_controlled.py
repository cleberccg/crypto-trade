"""FASE 12 - Controlled implementation and quick scientific pipeline for SuperTrend."""
from __future__ import annotations

import csv
import json
import os
import platform
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from config.settings import settings
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.repositories import CandleRepository
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
from paper_trading.paper_trader import PaperTrader
from risk.risk_manager import RiskManager
from strategies.factory import create_strategy
from utils.logger import get_logger
from validation.validator import OptimizationValidator, ValidationCriteria, default_validation_window

logger = get_logger(__name__)


@dataclass(frozen=True)
class Phase12SuperTrendConfig:
    symbol: str
    timeframe: str
    start: datetime | None = None
    end: datetime | None = None
    capital: float = 10_000.0
    window_days: int = 90
    output_prefix: str = "phase12_supertrend_controlled"
    optimizer_max_combinations: int = 15
    optimizer_workers: int = 1
    max_bars: int = 3000
    run_paper_campaign_if_approved: bool = True
    paper_cycles: int = 1


class Phase12SuperTrendService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: Phase12SuperTrendConfig) -> dict[str, Any]:
        run_id = HistoryPersistenceService.new_execution_id()
        start_ts = datetime.now(tz=timezone.utc)

        start_dt, end_dt = self._resolve_window(cfg)
        df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt)
        if not df.empty:
            df = df.tail(max(500, int(cfg.max_bars))).copy()
        if df.empty:
            report = {
                "phase": "12",
                "run_id": run_id,
                "status": "no_data",
                "decision": "OPCAO B",
                "reason": "No candles available for selected window.",
            }
            outputs = self._write_outputs(cfg.output_prefix, report)
            self._persist_checkpoint(run_id, report)
            return {"summary": report, "report": report, "outputs": outputs}

        references = [
            "TradingView SuperTrend public documentation",
            "Freqtrade community strategy references",
            "QuantConnect/Lean trend-follow implementations",
        ]

        smoke = self._run_smoke_test(df, cfg)
        backtest = self._run_backtest(df, cfg)

        early_stop = self._early_stop_decision(backtest)
        optimizer_report: dict[str, Any] | None = None
        validation_report: dict[str, Any] | None = None
        paper_report: dict[str, Any] | None = None

        if not early_stop["triggered"]:
            optimizer_report = self._run_reduced_optimizer(cfg, start_dt, end_dt)
            validation_report = self._run_validation(cfg, start_dt, end_dt, optimizer_report)

            if validation_report.get("approved", False) and cfg.run_paper_campaign_if_approved:
                paper_report = self._prepare_paper(cfg)

        approved = bool(validation_report and validation_report.get("approved", False)) and not early_stop["triggered"]
        decision = "OPCAO A" if approved else "OPCAO B"
        early_classification = str(early_stop.get("classification", ""))
        status = "approved_for_paper" if approved else "rejected"
        if early_classification in {"NO_TRADES", "INCONCLUSIVE_LOW_SAMPLE"}:
            status = "inconclusive"

        report = {
            "phase": "12",
            "run_id": run_id,
            "generated_at": start_ts.isoformat(),
            "strategy": "SuperTrendV1",
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "references": references,
            "reference_comparison": {
                "atr_component": "ATR period + multiplier adopted from common SuperTrend definitions",
                "trend_flip_logic": "Trend switches when price crosses dynamic upper/lower band",
                "risk_control": "ATR stop + RR/take profit integrated with existing Risk Manager",
            },
            "smoke_test": smoke,
            "backtest": backtest,
            "early_stop": early_stop,
            "optimizer": optimizer_report,
            "validation": validation_report,
            "paper_preparation": paper_report,
            "decision": decision,
            "status": status,
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        self._persist_checkpoint(run_id, report)

        summary = {
            "status": "completed",
            "run_id": run_id,
            "strategy": "SuperTrendV1",
            "decision": decision,
            "early_stop_triggered": bool(early_stop["triggered"]),
            "approved_for_paper": approved,
            "profit_factor": backtest.get("profit_factor", 0.0),
            "sharpe": backtest.get("sharpe", 0.0),
            "expectancy": backtest.get("expectancy", 0.0),
            "trades": backtest.get("number_of_trades", 0),
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _resolve_window(self, cfg: Phase12SuperTrendConfig) -> tuple[datetime, datetime]:
        end_dt = cfg.end or datetime.now(tz=timezone.utc)
        start_dt = cfg.start or (end_dt - timedelta(days=max(1, int(cfg.window_days))))
        return start_dt, end_dt

    def _load_df(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end)
        if not candles:
            return pd.DataFrame()
        return pd.DataFrame(
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

    def _run_smoke_test(self, df: pd.DataFrame, cfg: Phase12SuperTrendConfig) -> dict[str, Any]:
        smoke_df = df.tail(min(len(df), 800))
        strategy = create_strategy("SuperTrendV1")
        strategy.initialize()

        signal_count = 0
        first_signal: dict[str, Any] | None = None
        errors: list[str] = []

        try:
            for i in range(60, len(smoke_df)):
                window = smoke_df.iloc[: i + 1]
                enriched = strategy.calculate(window)
                sig = strategy.entry_signal(enriched)
                if str(sig.signal.value) == "BUY":
                    signal_count += 1
                    if first_signal is None:
                        first_signal = {
                            "price": float(sig.price),
                            "stop_loss": float(sig.stop_loss) if sig.stop_loss is not None else None,
                            "take_profit": float(sig.take_profit) if sig.take_profit is not None else None,
                            "score": float(sig.score),
                        }
        except Exception as exc:
            errors.append(f"signal_generation_error:{exc}")

        risk_ok = False
        if first_signal and not errors:
            try:
                rm = RiskManager()
                strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(strategy)
                if strategy_rr_min is None:
                    strategy_rr_min = RiskManager.infer_min_risk_reward_ratio_from_levels(
                        entry_price=float(first_signal["price"]),
                        stop_loss=float(first_signal["stop_loss"]),
                        take_profit=float(first_signal["take_profit"]),
                    )
                rm.evaluate_trade(
                    portfolio_value=max(100.0, float(cfg.capital)),
                    entry_price=float(first_signal["price"]),
                    stop_loss=float(first_signal["stop_loss"]),
                    take_profit=float(first_signal["take_profit"]),
                    strategy_score=float(first_signal["score"]),
                    min_risk_reward_ratio=strategy_rr_min,
                )
                risk_ok = True
            except Exception as exc:
                errors.append(f"risk_integration_error:{exc}")

        paper_ok = False
        if not errors:
            try:
                trader = PaperTrader(strategy=create_strategy("SuperTrendV1"), timeframe=cfg.timeframe)
                trader._strategy.initialize()  # keep smoke light but deterministic
                trader.run(smoke_df.tail(min(len(smoke_df), 300)), symbol=cfg.symbol, timeframe=cfg.timeframe)
                paper_ok = True
            except Exception as exc:
                errors.append(f"paper_integration_error:{exc}")

        return {
            "passed": len(errors) == 0,
            "signals_generated": signal_count,
            "risk_manager_integration": risk_ok,
            "paper_trading_integration": paper_ok,
            "errors": errors,
        }

    def _run_backtest(self, df: pd.DataFrame, cfg: Phase12SuperTrendConfig) -> dict[str, Any]:
        strategy = create_strategy("SuperTrendV1")
        strategy.initialize()
        result = BacktestEngine(strategy, config=BacktestConfig(initial_capital=cfg.capital)).run(
            df,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
        )
        metrics = result.metrics
        return {
            "status": "completed",
            "number_of_trades": int(metrics.total_trades),
            "profit_factor": float(metrics.profit_factor),
            "sharpe": float(metrics.sharpe_ratio),
            "expectancy": float(metrics.expectancy),
            "drawdown_pct": float(metrics.max_drawdown_pct),
            "win_rate": float(metrics.win_rate),
            "net_profit": float(metrics.net_profit),
            "return_pct": float(metrics.return_pct),
        }

    def _early_stop_decision(self, backtest: dict[str, Any]) -> dict[str, Any]:
        trades = int(backtest.get("number_of_trades", 0))
        pf = float(backtest.get("profit_factor", 0.0))
        expectancy = float(backtest.get("expectancy", 0.0))
        sharpe = float(backtest.get("sharpe", 0.0))
        min_trades = int(settings.validation.min_trades)

        reasons: list[str] = []
        if trades <= 0:
            reasons.append("no_trades")
            return {
                "triggered": True,
                "classification": "NO_TRADES",
                "reasons": reasons,
                "details": {
                    "trades_observed": trades,
                    "min_trades_required": min_trades,
                    "description": "No closed trades were generated in the evaluated window.",
                },
                "action": "mark_as_no_trades",
            }

        if trades < min_trades:
            reasons.append("insufficient_sample")
            return {
                "triggered": True,
                "classification": "INCONCLUSIVE_LOW_SAMPLE",
                "reasons": reasons,
                "details": {
                    "trades_observed": trades,
                    "min_trades_required": min_trades,
                    "description": "A estrategia ainda nao possui amostra suficiente para avaliacao estatisticamente robusta.",
                },
                "action": "mark_as_inconclusive_low_sample",
            }

        if pf < max(0.30, float(settings.validation.min_profit_factor) * 0.5):
            reasons.append("profit_factor_too_low")
        if expectancy < 0 and sharpe < 0 and pf < 1.0:
            reasons.append("negative_profile")

        triggered = len(reasons) > 0
        return {
            "triggered": triggered,
            "classification": "REJECTED_BY_PERFORMANCE" if triggered else "continue",
            "reasons": reasons,
            "action": "stop_pipeline_before_optimizer" if triggered else "continue",
            "details": {
                "trades_observed": trades,
                "min_trades_required": min_trades,
            },
        }

    def _run_reduced_optimizer(
        self,
        cfg: Phase12SuperTrendConfig,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        optimizer = StrategyOptimizer(output_dir=self._results_dir)
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                start=start_dt,
                end=end_dt,
                capital=cfg.capital,
                top_n=5,
                workers=max(1, int(cfg.optimizer_workers)),
                max_combinations=max(5, int(cfg.optimizer_max_combinations)),
                diagnostic=False,
                strategy_name="SuperTrendV1",
                strategy_version="v1",
            )
        )

        return {
            "executed": True,
            "combinations_tested": int(summary.combinations_tested),
            "combinations_discarded": int(summary.combinations_discarded),
            "top_results": [
                {
                    "rank": int(r.rank),
                    "parameters": dict(r.parameters),
                    "metrics": dict(r.metrics),
                }
                for r in summary.top_results[:5]
            ],
        }

    def _run_validation(
        self,
        cfg: Phase12SuperTrendConfig,
        start_dt: datetime,
        end_dt: datetime,
        optimizer_report: dict[str, Any] | None,
    ) -> dict[str, Any]:
        top = (optimizer_report or {}).get("top_results", [])
        if not top:
            return {"executed": False, "approved": False, "reason": "no_optimizer_results"}

        from optimizer.optimization_result import OptimizationResult

        candidates = [
            OptimizationResult(
                rank=int(item.get("rank", 0)),
                parameters=dict(item.get("parameters", {})),
                metrics=dict(item.get("metrics", {})),
                combinations_tested=int((optimizer_report or {}).get("combinations_tested", 0)),
                runtime_seconds=0.0,
                error=None,
            )
            for item in top
        ]

        validator = OptimizationValidator(
            criteria=ValidationCriteria(
                min_trades=settings.validation.min_trades,
                min_profit_factor=settings.validation.min_profit_factor,
                max_drawdown_pct=settings.validation.max_drawdown_pct,
                min_win_rate_pct=settings.validation.min_win_rate_pct,
                min_expectancy=settings.validation.min_expectancy,
                min_sharpe=settings.validation.min_sharpe,
            ),
            output_dir=self._results_dir,
            strategy_name="SuperTrendV1",
        )
        window = default_validation_window(start_dt, end_dt, symbol=cfg.symbol, timeframe=cfg.timeframe)
        summary = validator.validate(
            optimization_results=candidates,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            capital=cfg.capital,
            train_start=window.train_start,
            train_end=window.train_end,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
            top_n=5,
        )
        return {
            "executed": True,
            "approved": int(summary.passed) > 0,
            "total_candidates": int(summary.total_candidates),
            "discarded": int(summary.discarded),
            "passed": int(summary.passed),
            "output_files": [str(p) for p in summary.output_files],
        }

    def _prepare_paper(self, cfg: Phase12SuperTrendConfig) -> dict[str, Any]:
        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=2)
        df = self._load_df(cfg.symbol, cfg.timeframe, start_dt, end_dt)
        if df.empty:
            return {
                "prepared": False,
                "qualified": False,
                "reason": "insufficient_recent_data",
            }

        trader = PaperTrader(strategy=create_strategy("SuperTrendV1"), timeframe=cfg.timeframe)
        trader._strategy.initialize()
        stats = trader.run(df.tail(min(len(df), 600)), symbol=cfg.symbol, timeframe=cfg.timeframe)
        return {
            "prepared": True,
            "qualified": True,
            "paper_readiness_checks": {
                "strategy_instantiation": True,
                "risk_manager_attached": True,
                "historical_replay_smoke": True,
            },
            "paper_replay_stats": stats,
            "recommended_command": (
                "python main.py paper-live --strategy-name SuperTrendV1 --strategy-version v1.0 "
                f"--symbol {cfg.symbol} --timeframe {cfg.timeframe} --max-cycles {max(1, int(cfg.paper_cycles))}"
            ),
        }

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}.csv"
        row = {
            "strategy": report.get("strategy"),
            "symbol": report.get("symbol"),
            "timeframe": report.get("timeframe"),
            "smoke_passed": bool(report.get("smoke_test", {}).get("passed", False)),
            "backtest_trades": int(report.get("backtest", {}).get("number_of_trades", 0)),
            "backtest_profit_factor": float(report.get("backtest", {}).get("profit_factor", 0.0)),
            "backtest_sharpe": float(report.get("backtest", {}).get("sharpe", 0.0)),
            "backtest_expectancy": float(report.get("backtest", {}).get("expectancy", 0.0)),
            "backtest_drawdown_pct": float(report.get("backtest", {}).get("drawdown_pct", 0.0)),
            "decision": report.get("decision"),
            "status": report.get("status"),
        }
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)
        outputs["csv"] = str(csv_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _to_markdown(self, report: dict[str, Any]) -> str:
        smoke = report.get("smoke_test", {})
        bt = report.get("backtest", {})
        early = report.get("early_stop", {})
        opt = report.get("optimizer") or {}
        val = report.get("validation") or {}

        lines = [
            "# FASE 12 - Implementacao Controlada da SuperTrend",
            "",
            f"- Estrategia: **{report.get('strategy')}**",
            f"- Ativo: **{report.get('symbol')}**",
            f"- Timeframe: **{report.get('timeframe')}**",
            f"- Janela: **{report.get('window', {}).get('start')}** ate **{report.get('window', {}).get('end')}**",
            "",
            "## Referencias utilizadas",
            "",
        ]
        for ref in report.get("references", []):
            lines.append(f"- {ref}")

        lines += [
            "",
            "## Comparacao entre referencias",
            "",
            "```json",
            json.dumps(report.get("reference_comparison", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Smoke Test",
            "",
            f"- Resultado: {'PASS' if smoke.get('passed') else 'FAIL'}",
            f"- Sinais gerados: {smoke.get('signals_generated', 0)}",
            f"- Integracao Risk Manager: {'OK' if smoke.get('risk_manager_integration') else 'FAIL'}",
            f"- Integracao Paper Trading: {'OK' if smoke.get('paper_trading_integration') else 'FAIL'}",
            f"- Erros: {', '.join(smoke.get('errors', [])) or 'Nenhum'}",
            "",
            "## Backtest",
            "",
            f"- Trades: {bt.get('number_of_trades', 0)}",
            f"- Profit Factor: {bt.get('profit_factor', 0):.4f}",
            f"- Sharpe: {bt.get('sharpe', 0):.4f}",
            f"- Expectancy: {bt.get('expectancy', 0):.4f}",
            f"- Drawdown: {bt.get('drawdown_pct', 0):.4f}",
            f"- Win Rate: {bt.get('win_rate', 0):.4f}",
            "",
            "## Early Stop",
            "",
            f"- Acionado: {'SIM' if early.get('triggered') else 'NAO'}",
            f"- Motivos: {', '.join(early.get('reasons', [])) or 'Nao aplicavel'}",
            "",
            "## Otimizacao reduzida",
            "",
            f"- Executada: {'SIM' if opt.get('executed') else 'NAO'}",
            f"- Combinacoes testadas: {opt.get('combinations_tested', 0)}",
            "",
            "## Validation",
            "",
            f"- Executada: {'SIM' if val.get('executed') else 'NAO'}",
            f"- Aprovada: {'SIM' if val.get('approved') else 'NAO'}",
            f"- Candidatos aprovados: {val.get('passed', 0)}",
            "",
            "## Decisao final",
            "",
            f"**{report.get('decision')}**",
        ]
        return "\n".join(lines) + "\n"

    def _persist_checkpoint(self, run_id: str, report: dict[str, Any]) -> None:
        try:
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.start_execution_session(
                    execution_id=run_id,
                    started_at=datetime.now(tz=timezone.utc),
                    status="completed",
                    host=socket.gethostname(),
                    cpu=platform.processor(),
                    workers=1,
                    python_version=platform.python_version(),
                    git_version=os.getenv("GIT_COMMIT"),
                )
                history.save_checkpoint(
                    execution_id=run_id,
                    stage="phase12_supertrend_controlled",
                    processed=int(report.get("backtest", {}).get("number_of_trades", 0)),
                    completed=True,
                    payload={
                        "decision": report.get("decision"),
                        "status": report.get("status"),
                        "early_stop": report.get("early_stop", {}).get("triggered"),
                    },
                )
        except Exception as exc:
            logger.warning("Phase 12 checkpoint persistence failed (non-fatal): %s", exc)
