"""FASE 9.4 - First controlled operational improvement (V1.1)."""
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
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.repositories import CandleRepository
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService
from strategies.factory import create_strategy
from utils.logger import get_logger

logger = get_logger(__name__)

_TF_MINUTES: dict[str, int] = {
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
class Phase94Config:
    symbol: str | None = None
    timeframe: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    capital: float = 10_000.0
    window_days: int = 30
    output_prefix: str = "phase94_controlled_improvement"
    run_paper_campaign_if_approved: bool = True
    paper_cycles: int = 1


class Phase94ControlledImprovementService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: Phase94Config) -> dict[str, Any]:
        run_id = HistoryPersistenceService.new_execution_id()
        started_at = datetime.now(tz=timezone.utc)

        symbol, timeframe = self._resolve_market(cfg)
        start_dt, end_dt = self._resolve_window(cfg)
        tf_minutes = _TF_MINUTES.get(timeframe, 5)

        df = self._load_candles(symbol, timeframe, start_dt, end_dt)
        if df.empty:
            return {
                "summary": {
                    "status": "no_data",
                    "message": "No candle data available for selected window.",
                },
                "report": {},
                "outputs": {},
            }

        v10 = self._run_backtest("TradeOutcomeNextGenV1", df, symbol, timeframe, cfg.capital)
        v11 = self._run_backtest("TradeOutcomeNextGenV1.1", df, symbol, timeframe, cfg.capital)

        metrics_v10 = self._collect_operational_metrics("TradeOutcomeNextGenV1", v10, df, tf_minutes)
        metrics_v11 = self._collect_operational_metrics("TradeOutcomeNextGenV1.1", v11, df, tf_minutes)

        comparison = self._compare(metrics_v10, metrics_v11)
        approved = bool(comparison["approved"])

        campaign_result: dict[str, Any] | None = None
        if approved and cfg.run_paper_campaign_if_approved:
            service = PaperLiveService(base_dir=self._base_dir)
            campaign_result = service.run(
                PaperLiveConfig(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_name="TradeOutcomeNextGenV1.1",
                    strategy_version="v1.1",
                    initial_capital=max(100.0, float(cfg.capital)),
                    max_cycles=max(1, int(cfg.paper_cycles)),
                    poll_seconds=1.0,
                    resume=False,
                    output_prefix="phase94_v11_campaign",
                )
            )

        report = {
            "phase": "9.4",
            "run_id": run_id,
            "generated_at": started_at.isoformat(),
            "symbol": symbol,
            "timeframe": timeframe,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "capital": cfg.capital,
            "v1_0": metrics_v10,
            "v1_1": metrics_v11,
            "comparison": comparison,
            "paper_campaign": campaign_result,
            "rules": {
                "single_attempt": True,
                "no_new_strategy_family": True,
                "entry_logic_changed": False,
                "exit_logic_changed_only": True,
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        self._persist_checkpoint(run_id, report)

        summary = {
            "status": "completed",
            "run_id": run_id,
            "approved": approved,
            "decision": "usar_v1_1" if approved else "manter_v1_0",
            "question_v11_improved_v10": "Sim" if approved else "Não",
            "ready_for_paper_trading": "Sim" if approved else "Não",
            "recommendation": "Continuar operando V1.1" if approved else "Reverter para V1.0",
            "improved_metrics": comparison["improved_metrics"],
            "worsened_metrics": comparison["worsened_metrics"],
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _resolve_market(self, cfg: Phase94Config) -> tuple[str, str]:
        state_path = self._results_dir / "paper_live_state.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        symbol = cfg.symbol or str(state.get("symbol") or "BTC/USDT")
        timeframe = cfg.timeframe or str(state.get("timeframe") or "5m")
        return symbol, timeframe

    def _resolve_window(self, cfg: Phase94Config) -> tuple[datetime, datetime]:
        end_dt = cfg.end or datetime.now(tz=timezone.utc)
        start_dt = cfg.start or (end_dt - timedelta(days=max(1, int(cfg.window_days))))
        return start_dt, end_dt

    def _load_candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
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

    def _run_backtest(
        self,
        strategy_name: str,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        capital: float,
    ) -> Any:
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=capital))
        return engine.run(df, symbol=symbol, timeframe=timeframe)

    def _collect_operational_metrics(self, strategy_name: str, result: Any, df: pd.DataFrame, tf_minutes: int) -> dict[str, Any]:
        trades = result.trades
        metrics = result.metrics

        durations_min: list[float] = []
        blocked_bars = 0
        blocked_setups = 0
        total_bars = max(1, len(df) - 50)

        open_windows: list[tuple[int, int]] = []
        for t in trades:
            eb = int(t.get("entry_bar", 0))
            xb = int(t.get("exit_bar", eb))
            open_windows.append((eb, xb))
            durations_min.append(max(0.0, (xb - eb) * tf_minutes))
            blocked_bars += max(0, xb - eb)

        signal_strategy = create_strategy(strategy_name)
        signal_strategy.initialize()
        for i in range(50, len(df)):
            window = df.iloc[: i + 1]
            enriched = signal_strategy.calculate(window)
            sig = signal_strategy.entry_signal(enriched)
            if str(sig.signal.value) != "BUY":
                continue
            for eb, xb in open_windows:
                if eb < i <= xb:
                    blocked_setups += 1
                    break

        exit_eff_values: list[float] = []
        for t in trades:
            eb = int(t.get("entry_bar", 0))
            xb = int(t.get("exit_bar", eb))
            if xb <= eb or eb < 0 or xb >= len(df):
                continue
            segment = df.iloc[eb : xb + 1]
            entry = float(t.get("entry_price", 0.0))
            exit_price = float(t.get("exit_price", entry))
            if entry <= 0:
                continue
            mfe = (float(segment["high"].max()) - entry) / entry
            realized = (exit_price - entry) / entry
            eff = realized / mfe if mfe > 1e-10 else (1.0 if realized >= 0 else 0.0)
            exit_eff_values.append(eff)

        avg_duration_min = (sum(durations_min) / len(durations_min)) if durations_min else 0.0
        blocked_pct = blocked_bars / max(1, total_bars) * 100.0
        avg_blocked_setups_per_trade = blocked_setups / max(1, len(trades))
        exit_eff_mean = (sum(exit_eff_values) / len(exit_eff_values)) if exit_eff_values else 0.0

        return {
            "strategy": strategy_name,
            "profit_factor": float(metrics.profit_factor),
            "sharpe": float(metrics.sharpe_ratio),
            "expectancy": float(metrics.expectancy),
            "drawdown_pct": float(metrics.max_drawdown_pct),
            "net_profit": float(metrics.net_profit),
            "win_rate": float(metrics.win_rate),
            "number_of_trades": int(metrics.total_trades),
            "avg_duration_minutes": round(avg_duration_min, 2),
            "avg_duration_hours": round(avg_duration_min / 60.0, 3),
            "blocked_time_pct": round(blocked_pct, 2),
            "avg_blocked_setups_per_trade": round(avg_blocked_setups_per_trade, 3),
            "exit_efficiency": round(exit_eff_mean, 4),
            "return_pct": float(metrics.return_pct),
            "final_capital": float(metrics.final_capital),
        }

    def _compare(self, v10: dict[str, Any], v11: dict[str, Any]) -> dict[str, Any]:
        improved: list[str] = []
        worsened: list[str] = []

        def _cmp(key: str, higher_is_better: bool = True, label: str | None = None) -> None:
            name = label or key
            a = float(v10.get(key, 0.0))
            b = float(v11.get(key, 0.0))
            if higher_is_better:
                if b > a:
                    improved.append(name)
                elif b < a:
                    worsened.append(name)
            else:
                if b < a:
                    improved.append(name)
                elif b > a:
                    worsened.append(name)

        _cmp("profit_factor", True, "Profit Factor")
        _cmp("sharpe", True, "Sharpe")
        _cmp("expectancy", True, "Expectancy")
        _cmp("drawdown_pct", False, "Drawdown")
        _cmp("net_profit", True, "Net Profit")
        _cmp("win_rate", True, "Win Rate")
        _cmp("number_of_trades", True, "Número de trades")
        _cmp("avg_duration_hours", False, "Permanência média")
        _cmp("blocked_time_pct", False, "Tempo bloqueado")
        _cmp("exit_efficiency", True, "Eficiência de saída")

        approved = (
            float(v11["profit_factor"]) > float(v10["profit_factor"])
            and float(v11["expectancy"]) > float(v10["expectancy"])
            and float(v11["net_profit"]) > float(v10["net_profit"])
            and float(v11["blocked_time_pct"]) < float(v10["blocked_time_pct"])
            and int(v11["number_of_trades"]) >= int(v10["number_of_trades"])
            and float(v11["drawdown_pct"]) <= float(v10["drawdown_pct"])
        )

        return {
            "approved": approved,
            "improved_metrics": improved,
            "worsened_metrics": worsened,
            "v11_better_than_v10": "Sim" if approved else "Não",
            "ready_for_paper_trading": "Sim" if approved else "Não",
            "recommendation": "Continuar operando" if approved else "Reverter para V1.0",
        }

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}_metrics.csv"
        rows = [report.get("v1_0", {}), report.get("v1_1", {})]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        outputs["csv"] = str(csv_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _to_markdown(self, report: dict[str, Any]) -> str:
        v10 = report.get("v1_0", {})
        v11 = report.get("v1_1", {})
        comp = report.get("comparison", {})
        lines = [
            "# FASE 9.4 — Primeira Melhoria Operacional Controlada (V1.1)",
            "",
            f"- Ativo: **{report.get('symbol')}**",
            f"- Timeframe: **{report.get('timeframe')}**",
            f"- Janela: **{report.get('start')}** até **{report.get('end')}**",
            "",
            "## V1.0 vs V1.1",
            "",
            "| Métrica | V1.0 | V1.1 |",
            "|---|---:|---:|",
            f"| Profit Factor | {v10.get('profit_factor', 0):.4f} | {v11.get('profit_factor', 0):.4f} |",
            f"| Sharpe | {v10.get('sharpe', 0):.4f} | {v11.get('sharpe', 0):.4f} |",
            f"| Expectancy | {v10.get('expectancy', 0):.4f} | {v11.get('expectancy', 0):.4f} |",
            f"| Drawdown % | {v10.get('drawdown_pct', 0):.4f} | {v11.get('drawdown_pct', 0):.4f} |",
            f"| Net Profit | {v10.get('net_profit', 0):.2f} | {v11.get('net_profit', 0):.2f} |",
            f"| Win Rate | {v10.get('win_rate', 0):.4f} | {v11.get('win_rate', 0):.4f} |",
            f"| Número de trades | {v10.get('number_of_trades', 0)} | {v11.get('number_of_trades', 0)} |",
            f"| Permanência média (h) | {v10.get('avg_duration_hours', 0):.3f} | {v11.get('avg_duration_hours', 0):.3f} |",
            f"| Tempo bloqueado % | {v10.get('blocked_time_pct', 0):.2f} | {v11.get('blocked_time_pct', 0):.2f} |",
            f"| Eficiência de saída | {v10.get('exit_efficiency', 0):.4f} | {v11.get('exit_efficiency', 0):.4f} |",
            "",
            "## Decisão",
            "",
            f"- A V1.1 melhorou a V1.0? **{comp.get('v11_better_than_v10', 'Não')}**",
            f"- Métricas melhoradas: {', '.join(comp.get('improved_metrics', [])) or 'Nenhuma'}",
            f"- Métricas pioradas: {', '.join(comp.get('worsened_metrics', [])) or 'Nenhuma'}",
            f"- Estratégia pronta para continuar em Paper Trading? **{comp.get('ready_for_paper_trading', 'Não')}**",
            f"- Recomendação: **{comp.get('recommendation', 'Reverter para V1.0')}**",
            "",
            "## Regra de Controle",
            "",
            "- Tentativa única executada (sem V1.2/V1.3/V2 automáticos).",
        ]
        return "\n".join(lines) + "\n"

    def _persist_checkpoint(self, run_id: str, report: dict[str, Any]) -> None:
        try:
            started_at = datetime.now(tz=timezone.utc)
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.start_execution_session(
                    execution_id=run_id,
                    started_at=started_at,
                    status="completed",
                    host=socket.gethostname(),
                    cpu=platform.processor(),
                    workers=1,
                    python_version=platform.python_version(),
                    git_version=os.getenv("GIT_COMMIT"),
                )
                history.save_checkpoint(
                    execution_id=run_id,
                    stage="phase94_controlled_improvement",
                    processed=int(report.get("v1_1", {}).get("number_of_trades", 0)),
                    completed=True,
                    payload={
                        "approved": report.get("comparison", {}).get("approved"),
                        "decision": report.get("comparison", {}).get("recommendation"),
                        "improved_metrics": report.get("comparison", {}).get("improved_metrics", []),
                        "worsened_metrics": report.get("comparison", {}).get("worsened_metrics", []),
                    },
                )
        except Exception as exc:
            logger.warning("Phase 9.4 checkpoint persistence failed (non-fatal): %s", exc)
