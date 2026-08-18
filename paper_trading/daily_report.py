"""Daily operational report generator for paper trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import csv
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PaperDailyReportConfig:
    report_date: date
    strategy_name: str | None = None
    strategy_version: str | None = None
    output_prefix: str = "paper_trading_daily_report"


class PaperDailyReportService:
    """Builds a daily operational report from persisted paper trading data."""

    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, cfg: PaperDailyReportConfig) -> dict[str, Any]:
        start_dt = datetime.combine(cfg.report_date, time.min).replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1)

        params: dict[str, Any] = {"start_dt": start_dt, "end_dt": end_dt}
        strategy_hist_filter = ""
        strategy_legacy_filter = ""
        if cfg.strategy_name:
            if cfg.strategy_version:
                params["strategy_name"] = f"{cfg.strategy_name}@{cfg.strategy_version}"
                strategy_hist_filter = " AND strategy = :strategy_name "
            else:
                params["strategy_name_prefix"] = f"{cfg.strategy_name}@%"
                params["strategy_name"] = cfg.strategy_name
                strategy_hist_filter = " AND (strategy = :strategy_name OR strategy LIKE :strategy_name_prefix) "
            strategy_legacy_filter = " AND strategy_name = :strategy_name "

        closed_trades = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT id, execution_id, strategy, symbol, timeframe, side, entry_time, exit_time,
                       entry_price, exit_price, stop_loss, take_profit, risk_reward,
                       quantity, pnl, pnl_percent, duration_minutes, exit_reason, score
                FROM trade_history
                WHERE exit_time IS NOT NULL
                  AND exit_time >= :start_dt
                  AND exit_time < :end_dt
                  {strategy_filter}
                ORDER BY exit_time ASC
                """.replace("{strategy_filter}", strategy_hist_filter)
            ),
            params,
        ).mappings().all()]

        entry_trades = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT id, execution_id, strategy, symbol, timeframe, side, entry_time, exit_time,
                       entry_price, exit_price, stop_loss, take_profit, risk_reward,
                       quantity, pnl, pnl_percent, duration_minutes, exit_reason, score
                FROM trade_history
                WHERE entry_time >= :start_dt
                  AND entry_time < :end_dt
                  {strategy_filter}
                ORDER BY entry_time ASC
                """.replace("{strategy_filter}", strategy_hist_filter)
            ),
            params,
        ).mappings().all()]

        open_positions = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT id, symbol, strategy_name, status, entry_price, quantity, stop_loss,
                       take_profit, entry_time
                FROM trades
                WHERE is_paper = 1
                  AND status = 'OPEN'
                  AND entry_time >= :start_dt
                  AND entry_time < :end_dt
                  {strategy_filter}
                ORDER BY entry_time ASC
                """.replace("{strategy_filter}", strategy_legacy_filter)
            ),
            params,
        ).mappings().all()]

        signals = [dict(row) for row in self._session.execute(
            text(
                """
                  SELECT execution_id, strategy, symbol, timeframe, timestamp, `signal` AS signal_type,
                       score, entry_price, stop_loss, take_profit, rr, accepted,
                       rejection_reason, market_regime
                FROM signal_snapshots
                WHERE timestamp >= :start_dt
                  AND timestamp < :end_dt
                  {strategy_filter}
                ORDER BY timestamp ASC
                """.replace("{strategy_filter}", strategy_hist_filter)
            ),
            params,
        ).mappings().all()]

        snapshots = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT timestamp, total_value, cash, positions_value, open_trades
                FROM portfolio_snapshots
                WHERE source = 'paper'
                  AND timestamp >= :start_dt
                  AND timestamp < :end_dt
                ORDER BY timestamp ASC
                """
            ),
            params,
        ).mappings().all()]

        campaign_overview = self._load_recent_campaign_overview()

        summary = self._build_summary(
            report_date=cfg.report_date,
            closed_trades=closed_trades,
            entry_trades=entry_trades,
            open_positions=open_positions,
            signals=signals,
            snapshots=snapshots,
            strategy_name=cfg.strategy_name,
            campaign_overview=campaign_overview,
        )

        outputs = self._write_outputs(summary=summary, cfg=cfg)
        return {"summary": summary, "outputs": outputs}

    def _build_summary(
        self,
        *,
        report_date: date,
        closed_trades: list[dict[str, Any]],
        entry_trades: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
        strategy_name: str | None,
        campaign_overview: dict[str, Any] | None,
    ) -> dict[str, Any]:
        pnl_values = [float(t.get("pnl") or 0.0) for t in closed_trades]
        winning = [p for p in pnl_values if p > 0]
        losing = [p for p in pnl_values if p <= 0]

        total_trades = len(closed_trades)
        win_rate = (len(winning) / total_trades) if total_trades else 0.0
        gross_profit = sum(winning)
        gross_loss_abs = abs(sum(losing))
        net_profit = sum(pnl_values)
        profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)
        expectancy = (net_profit / total_trades) if total_trades else 0.0

        returns = [float(t.get("pnl_percent") or 0.0) for t in closed_trades]
        sharpe = 0.0
        if len(returns) > 1:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
            std = variance ** 0.5
            if std > 0:
                sharpe = mean_ret / std

        initial_capital = float(snapshots[0].get("total_value")) if snapshots else 0.0
        final_capital = float(snapshots[-1].get("total_value")) if snapshots else initial_capital
        max_drawdown = self._compute_max_drawdown([float(s.get("total_value")) for s in snapshots])

        accepted_entries = [s for s in signals if s.get("signal_type") == "BUY" and bool(s.get("accepted"))]
        rejected_entries = [s for s in signals if s.get("signal_type") == "BUY" and not bool(s.get("accepted"))]

        best_trade = max(closed_trades, key=lambda t: float(t.get("pnl") or 0.0), default=None)
        worst_trade = min(closed_trades, key=lambda t: float(t.get("pnl") or 0.0), default=None)

        pnl_by_asset: dict[str, float] = {}
        pnl_by_hour: dict[str, float] = {}
        pnl_by_timeframe: dict[str, float] = {}
        pnl_by_regime: dict[str, float] = {}

        regime_lookup: dict[tuple[str, Any], str] = {}
        entry_reason_lookup: dict[tuple[str, Any], str] = {}
        for sig in accepted_entries:
            key = (str(sig.get("symbol")), sig.get("timestamp"))
            regime = sig.get("market_regime")
            if regime:
                regime_lookup[key] = str(regime)
            entry_reason_lookup[key] = self._signal_entry_reason(sig, strategy_name)

        for trade in closed_trades:
            pnl = float(trade.get("pnl") or 0.0)
            symbol = str(trade.get("symbol"))
            pnl_by_asset[symbol] = pnl_by_asset.get(symbol, 0.0) + pnl

            entry_time = trade.get("entry_time")
            hour_key = entry_time.astimezone(timezone.utc).strftime("%H:00") if hasattr(entry_time, "astimezone") else "unknown"
            pnl_by_hour[hour_key] = pnl_by_hour.get(hour_key, 0.0) + pnl

            timeframe = str(trade.get("timeframe") or "unknown")
            pnl_by_timeframe[timeframe] = pnl_by_timeframe.get(timeframe, 0.0) + pnl

            regime = regime_lookup.get((symbol, entry_time))
            if regime:
                pnl_by_regime[regime] = pnl_by_regime.get(regime, 0.0) + pnl

        diagnostics = self._build_diagnostics(
            total_trades=total_trades,
            net_profit=net_profit,
            profit_factor=profit_factor,
            rejected_entries=len(rejected_entries),
            open_trades=len(open_positions),
        )

        operation_log = self._build_operation_log(closed_trades, entry_reason_lookup)

        return {
            "report_date": report_date.isoformat(),
            "campaign_overview": campaign_overview or {},
            "performance": {
                "number_of_operations": total_trades,
                "entries": len(entry_trades),
                "exits": total_trades,
                "cancelations": 0,
                "rejected_entries": len(rejected_entries),
                "open_positions": len(open_positions),
                "win_rate": round(win_rate, 4),
                "net_profit": round(net_profit, 4),
                "profit_factor": round(profit_factor, 4),
                "sharpe": round(sharpe, 4),
                "expectancy": round(expectancy, 4),
                "drawdown": round(max_drawdown, 4),
                "final_capital": round(final_capital, 4),
                "initial_capital": round(initial_capital, 4),
            },
            "quality": {
                "best_trade": self._trade_to_dict(best_trade),
                "worst_trade": self._trade_to_dict(worst_trade),
                "most_profitable_assets": self._top_items(pnl_by_asset, reverse=True),
                "least_profitable_assets": self._top_items(pnl_by_asset, reverse=False),
                "most_profitable_hours": self._top_items(pnl_by_hour, reverse=True),
                "least_profitable_hours": self._top_items(pnl_by_hour, reverse=False),
                "most_profitable_timeframes": self._top_items(pnl_by_timeframe, reverse=True),
                "least_profitable_timeframes": self._top_items(pnl_by_timeframe, reverse=False),
                "most_profitable_regimes": self._top_items(pnl_by_regime, reverse=True),
                "least_profitable_regimes": self._top_items(pnl_by_regime, reverse=False),
            },
            "diagnostic": diagnostics,
            "operations": operation_log,
        }

    def _write_outputs(self, summary: dict[str, Any], cfg: PaperDailyReportConfig) -> dict[str, str]:
        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)

        base = f"{cfg.output_prefix}_latest"

        json_path = out_dir / f"{base}.json"
        md_path = out_dir / f"{base}.md"
        csv_path = out_dir / f"{base}.operations.csv"

        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(summary), encoding="utf-8")
        self._write_operations_csv(csv_path, summary.get("operations", []))

        return {
            "report_json": str(json_path),
            "report_md": str(md_path),
            "operations_csv": str(csv_path),
        }

    def _build_operation_log(
        self,
        closed_trades: list[dict[str, Any]],
        entry_reason_lookup: dict[tuple[str, Any], str],
    ) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for trade in closed_trades:
            symbol = str(trade.get("symbol"))
            entry_time = trade.get("entry_time")
            exit_time = trade.get("exit_time")
            operations.append(
                {
                    "execution_id": trade.get("execution_id"),
                    "symbol": symbol,
                    "timeframe": str(trade.get("timeframe") or "unknown"),
                    "entry_time": entry_time.isoformat() if hasattr(entry_time, "isoformat") else None,
                    "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else None,
                    "entry_price": trade.get("entry_price"),
                    "exit_price": trade.get("exit_price"),
                    "stop_loss": trade.get("stop_loss"),
                    "take_profit": trade.get("take_profit"),
                    "duration_minutes": trade.get("duration_minutes"),
                    "profit": trade.get("pnl"),
                    "loss": min(float(trade.get("pnl") or 0.0), 0.0),
                    "entry_reason": entry_reason_lookup.get((symbol, entry_time), "strategy_signal_entry"),
                    "exit_reason": trade.get("exit_reason"),
                }
            )
        return operations

    @staticmethod
    def _write_operations_csv(csv_path: Path, operations: list[dict[str, Any]]) -> None:
        fieldnames = [
            "execution_id",
            "symbol",
            "timeframe",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "stop_loss",
            "take_profit",
            "duration_minutes",
            "profit",
            "loss",
            "entry_reason",
            "exit_reason",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in operations:
                writer.writerow(row)

    @staticmethod
    def _signal_entry_reason(signal: dict[str, Any], strategy_name: str | None) -> str:
        if strategy_name == "TradeOutcomeNextGenV1":
            return "distance_to_ema_pct<=0.162026"
        score = signal.get("score")
        return f"strategy_signal_score={score}" if score is not None else "strategy_signal_entry"

    @staticmethod
    def _compute_max_drawdown(equity_values: list[float]) -> float:
        if not equity_values:
            return 0.0
        peak = equity_values[0]
        max_dd = 0.0
        for value in equity_values:
            if value > peak:
                peak = value
            if peak > 0:
                dd = (peak - value) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    @staticmethod
    def _trade_to_dict(trade: dict[str, Any] | None) -> dict[str, Any] | None:
        if trade is None:
            return None
        entry_time = trade.get("entry_time")
        exit_time = trade.get("exit_time")
        return {
            "id": trade.get("id"),
            "symbol": trade.get("symbol"),
            "entry_time": entry_time.isoformat() if hasattr(entry_time, "isoformat") else None,
            "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else None,
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "pnl": trade.get("pnl"),
            "pnl_pct": trade.get("pnl_percent"),
            "exit_reason": trade.get("exit_reason"),
            "duration_minutes": trade.get("duration_minutes"),
        }

    @staticmethod
    def _top_items(values: dict[str, float], reverse: bool) -> list[dict[str, Any]]:
        if not values:
            return []
        ranked = sorted(values.items(), key=lambda item: item[1], reverse=reverse)
        return [{"name": name, "pnl": round(pnl, 4)} for name, pnl in ranked[:5]]

    @staticmethod
    def _build_diagnostics(
        *,
        total_trades: int,
        net_profit: float,
        profit_factor: float,
        rejected_entries: int,
        open_trades: int,
    ) -> dict[str, str]:
        worked = "Geracao de lucro liquido positiva no dia." if net_profit > 0 else "Protecao de risco ativa sem ganho liquido positivo."
        not_worked = "Efetividade de saidas precisa ajuste (Profit Factor abaixo de 1.0)." if profit_factor < 1.0 else "Sem falha estrutural evidente nas saidas."

        if rejected_entries > 0:
            missed = f"{rejected_entries} entradas foram bloqueadas por risco; revisar se filtros estao restritivos demais."
        else:
            missed = "Nao houve entradas rejeitadas por risco no periodo."

        operational_error = "Ha posicoes abertas no fechamento do dia; validar rotina de encerramento." if open_trades > 0 else "Nenhum erro operacional evidente nos registros do dia."

        filter_adjustment = "Ajuste de filtro recomendado apenas apos observar janela movel (7/14/30 dias)." if total_trades > 0 else "Sem trades suficientes hoje; manter estrategia congelada e coletar mais dados."

        return {
            "what_worked_today": worked,
            "what_did_not_work_today": not_worked,
            "operational_error_detected": operational_error,
            "missed_opportunity": missed,
            "filter_adjustment_recommendation": filter_adjustment,
        }

    @staticmethod
    def _to_markdown(summary: dict[str, Any]) -> str:
        perf = summary["performance"]
        quality = summary["quality"]
        diag = summary["diagnostic"]

        lines = [
            "# Paper Trading Daily Report",
            "",
            f"Date: {summary['report_date']}",
            "",
        ]

        overview = summary.get("campaign_overview") or {}
        if overview:
            baseline = overview.get("baseline_official") or {}
            answers = overview.get("answers") or {}
            lines.extend([
                "## Campaign Overview",
                f"- Implemented: {overview.get('implemented_count', 0)}",
                f"- Evaluated: {overview.get('stage_counters', {}).get('backtest_reached', 0)}",
                f"- In Validation: {overview.get('stage_counters', {}).get('validation_reached', 0)}",
                f"- In Paper Candidate: {overview.get('paper_candidate_count', 0)}",
                f"- In Paper Experimental: {overview.get('paper_experimental_started_count', 0)}",
                f"- Baseline strategy: {baseline.get('strategy', 'ClassicEMACrossover')}",
                f"- Baseline superou: {baseline.get('superou', answers.get('baseline_superou', 0))}",
                f"- Baseline empatou: {baseline.get('empatou', answers.get('baseline_empatou', 0))}",
                f"- Baseline abaixo: {baseline.get('abaixo', answers.get('baseline_abaixo', 0))}",
                "",
            ])

        lines.extend([
            "## Performance",
            f"- Number of operations: {perf['number_of_operations']}",
            f"- Entries: {perf['entries']}",
            f"- Exits: {perf['exits']}",
            f"- Cancelations: {perf['cancelations']}",
            f"- Rejected entries: {perf['rejected_entries']}",
            f"- Open positions: {perf['open_positions']}",
            f"- Win rate: {perf['win_rate']}",
            f"- Net profit: {perf['net_profit']}",
            f"- Profit Factor: {perf['profit_factor']}",
            f"- Sharpe: {perf['sharpe']}",
            f"- Expectancy: {perf['expectancy']}",
            f"- Drawdown: {perf['drawdown']}",
            f"- Final capital: {perf['final_capital']}",
            "",
            "## Quality",
            f"- Best trade: {json.dumps(quality['best_trade'], ensure_ascii=False)}",
            f"- Worst trade: {json.dumps(quality['worst_trade'], ensure_ascii=False)}",
            f"- Most profitable assets: {json.dumps(quality['most_profitable_assets'], ensure_ascii=False)}",
            f"- Least profitable assets: {json.dumps(quality['least_profitable_assets'], ensure_ascii=False)}",
            f"- Most profitable hours: {json.dumps(quality['most_profitable_hours'], ensure_ascii=False)}",
            f"- Least profitable hours: {json.dumps(quality['least_profitable_hours'], ensure_ascii=False)}",
            f"- Most profitable timeframes: {json.dumps(quality['most_profitable_timeframes'], ensure_ascii=False)}",
            f"- Least profitable timeframes: {json.dumps(quality['least_profitable_timeframes'], ensure_ascii=False)}",
            f"- Most profitable regimes: {json.dumps(quality['most_profitable_regimes'], ensure_ascii=False)}",
            f"- Least profitable regimes: {json.dumps(quality['least_profitable_regimes'], ensure_ascii=False)}",
            "",
            "## Diagnostic",
            f"- What worked today: {diag['what_worked_today']}",
            f"- What did not work today: {diag['what_did_not_work_today']}",
            f"- Operational error detected: {diag['operational_error_detected']}",
            f"- Missed opportunity: {diag['missed_opportunity']}",
            f"- Filter adjustment recommendation: {diag['filter_adjustment_recommendation']}",
            "",
            "## Operations",
            f"- Total operation rows: {len(summary.get('operations', []))}",
            "",
        ])
        return "\n".join(lines)

    def _load_recent_campaign_overview(self) -> dict[str, Any] | None:
        out_dir = self._base_dir / "optimization" / "results"
        if not out_dir.exists():
            return None

        candidates: list[Path] = []
        for pattern in ("overnight*.json", "consolidacao*.json", "phase13*.json"):
            candidates.extend(out_dir.glob(pattern))

        json_candidates = [p for p in candidates if p.is_file()]
        if not json_candidates:
            return None

        latest = max(json_candidates, key=lambda path: path.stat().st_mtime)
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        overview_keys = {
            "implemented_count",
            "rejected_count",
            "approved_count",
            "in_paper_trading_count",
            "paper_candidate_count",
            "paper_experimental_started_count",
            "paper_experimental_running_count",
            "stage_counters",
            "answers",
            "baseline_official",
            "overnight_v2",
            "stop_reason",
        }
        overview = {key: data.get(key) for key in overview_keys if key in data}
        if not overview:
            return None
        overview["source_file"] = str(latest)
        overview["generated_at"] = data.get("generated_at")
        overview["run_id"] = data.get("run_id")
        return overview
