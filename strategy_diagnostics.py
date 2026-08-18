"""Permanent strategy diagnostics for paper/live operation."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
import platform
import socket
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from database.history_service import HistoryPersistenceService
from paper_trading.paper_broker import PaperBroker
from risk.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy, SignalType
from strategies.factory import create_strategy
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyDiagnosticsConfig:
    strategy_name: str | None = None
    strategy_version: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    execution_id: str | None = None
    window_hours: int = 24
    window_days: int = 7
    output_prefix: str = "strategy_diagnostics"


class StrategyDiagnosticsService:
    """Audits the operational chain from candles to closed trades."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._results_dir / "paper_live_state.json"

    def run(self, cfg: StrategyDiagnosticsConfig) -> dict[str, Any]:
        started_at = datetime.now(tz=timezone.utc)
        diagnostic_execution_id = HistoryPersistenceService.new_execution_id()

        with get_session() as session:
            history = HistoryPersistenceService(session)
            history.start_execution_session(
                execution_id=diagnostic_execution_id,
                started_at=started_at,
                status="running",
                host=socket.gethostname(),
                cpu=platform.processor(),
                workers=1,
                python_version=platform.python_version(),
                git_version=os.getenv("GIT_COMMIT"),
            )

        try:
            context = self._resolve_context(cfg)
            strategy_name = context["strategy_name"]
            strategy_version = context["strategy_version"]
            symbol = context["symbol"]
            timeframe = context["timeframe"]
            source_execution_id = context.get("execution_id")
            session_start = context.get("session_start")
            session_end = context.get("session_end") or datetime.now(tz=timezone.utc)

            now = datetime.now(tz=timezone.utc)
            scopes = [
                {
                    "scope": "latest_session",
                    "label": "Última sessão Paper Live",
                    "start": session_start,
                    "end": session_end,
                },
                {
                    "scope": "last_24h",
                    "label": "Últimas 24 horas",
                    "start": now - timedelta(hours=max(1, int(cfg.window_hours))),
                    "end": now,
                },
                {
                    "scope": "last_7d",
                    "label": "Últimos 7 dias",
                    "start": now - timedelta(days=max(1, int(cfg.window_days))),
                    "end": now,
                },
            ]

            scope_results = [
                self._diagnose_scope(
                    strategy_name=strategy_name,
                    strategy_version=strategy_version,
                    symbol=symbol,
                    timeframe=timeframe,
                    scope=str(scope["scope"]),
                    label=str(scope["label"]),
                    start_dt=scope["start"],
                    end_dt=scope["end"],
                )
                for scope in scopes
            ]

            heatmap_rows = self._build_heatmap_rows(strategy_name, strategy_version, scopes[-1]["start"], scopes[-1]["end"])
            decision = self._make_decision(scope_results)
            report = {
                "generated_at": started_at.isoformat(),
                "diagnostic_execution_id": diagnostic_execution_id,
                "source_execution_id": source_execution_id,
                "strategy_name": strategy_name,
                "strategy_version": strategy_version,
                "symbol": symbol,
                "timeframe": timeframe,
                "context": context,
                "scopes": scope_results,
                "heatmap": heatmap_rows,
                "rejection_ranking": self._aggregate_rejections(scope_results),
                "decision": decision,
            }

            outputs = self._write_outputs(report, cfg.output_prefix, started_at)
            self._persist_checkpoint(
                diagnostic_execution_id=diagnostic_execution_id,
                source_execution_id=source_execution_id,
                report=report,
                outputs=outputs,
                processed=int(scope_results[0]["pipeline"]["candles_analyzed"]["count"]),
            )

            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.finish_execution_session(
                    execution_id=diagnostic_execution_id,
                    finished_at=datetime.now(tz=timezone.utc),
                    duration=(datetime.now(tz=timezone.utc) - started_at).total_seconds(),
                    status="completed",
                )

            return {"report": report, "outputs": outputs}
        except Exception:
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.finish_execution_session(
                    execution_id=diagnostic_execution_id,
                    finished_at=datetime.now(tz=timezone.utc),
                    duration=(datetime.now(tz=timezone.utc) - started_at).total_seconds(),
                    status="failed",
                )
            raise

    def _resolve_context(self, cfg: StrategyDiagnosticsConfig) -> dict[str, Any]:
        state = self._load_state()
        execution_id = cfg.execution_id or state.get("execution_id")
        strategy_name = cfg.strategy_name or state.get("strategy_name")
        strategy_version = cfg.strategy_version or state.get("strategy_version")
        symbol = cfg.symbol or state.get("symbol")
        timeframe = cfg.timeframe or state.get("timeframe")

        if execution_id:
            with get_session() as session:
                session_row = session.execute(
                    text(
                        """
                        SELECT started_at, finished_at, status
                        FROM execution_sessions
                        WHERE execution_id = :execution_id
                        LIMIT 1
                        """
                    ),
                    {"execution_id": execution_id},
                ).fetchone()

                if session_row is not None:
                    if strategy_name is None or strategy_version is None or symbol is None or timeframe is None:
                        meta_row = self._latest_history_metadata(session, execution_id)
                        if meta_row is not None:
                            inferred_name, inferred_version = self._split_strategy_key(str(meta_row[0]))
                            strategy_name = strategy_name or inferred_name
                            strategy_version = strategy_version or inferred_version
                            symbol = symbol or str(meta_row[1])
                            timeframe = timeframe or str(meta_row[2])

                    return {
                        "execution_id": execution_id,
                        "strategy_name": strategy_name or "TradeOutcomeNextGenV1",
                        "strategy_version": strategy_version or "v1.0",
                        "symbol": symbol or "BTC/USDT",
                        "timeframe": timeframe or "5m",
                        "session_start": self._to_datetime(session_row[0]),
                        "session_end": self._to_datetime(session_row[1]) if session_row[1] is not None else self._to_datetime(state.get("last_open_time")) if state.get("last_open_time") else None,
                        "session_status": str(session_row[2]),
                        "state": state,
                    }

        with get_session() as session:
            latest_execution_id = session.execute(
                text(
                    """
                    SELECT execution_id
                    FROM (
                        SELECT execution_id, MAX(timestamp) AS activity_at
                        FROM signal_snapshots
                        GROUP BY execution_id
                        UNION ALL
                        SELECT execution_id, MAX(exit_time) AS activity_at
                        FROM trade_history
                        GROUP BY execution_id
                    ) AS activity
                    ORDER BY activity_at DESC
                    LIMIT 1
                    """
                )
            ).fetchone()

            if latest_execution_id is None:
                raise RuntimeError("Nenhuma sessao Paper Live encontrada para diagnosticar.")

            execution_id = str(latest_execution_id[0])
            meta_row = self._latest_history_metadata(session, execution_id)
            if meta_row is None:
                raise RuntimeError("Nao foi possivel inferir estrategia/ativo/timeframe da ultima sessao Paper Live.")

            inferred_name, inferred_version = self._split_strategy_key(str(meta_row[0]))
            strategy_name = strategy_name or inferred_name
            strategy_version = strategy_version or inferred_version
            symbol = symbol or str(meta_row[1])
            timeframe = timeframe or str(meta_row[2])

            return {
                "execution_id": execution_id,
                "strategy_name": strategy_name,
                "strategy_version": strategy_version,
                "symbol": symbol,
                "timeframe": timeframe,
                "session_start": None,
                "session_end": None,
                "session_status": None,
                "state": state,
            }

    def _latest_history_metadata(self, session: Any, execution_id: str) -> tuple[Any, Any, Any] | None:
        row = session.execute(
            text(
                """
                SELECT strategy, symbol, timeframe
                FROM trade_history
                WHERE execution_id = :execution_id
                ORDER BY exit_time DESC
                LIMIT 1
                """
            ),
            {"execution_id": execution_id},
        ).fetchone()
        if row is not None:
            return row

        row = session.execute(
            text(
                """
                SELECT strategy, symbol, timeframe
                FROM signal_snapshots
                WHERE execution_id = :execution_id
                ORDER BY timestamp DESC
                LIMIT 1
                """
            ),
            {"execution_id": execution_id},
        ).fetchone()
        return row

    @staticmethod
    def _split_strategy_key(strategy_key: str) -> tuple[str, str]:
        if "@" in strategy_key:
            name, version = strategy_key.split("@", 1)
            return name or "TradeOutcomeNextGenV1", version or "v1.0"
        return strategy_key or "TradeOutcomeNextGenV1", "v1.0"

    def _diagnose_scope(
        self,
        *,
        strategy_name: str,
        strategy_version: str,
        symbol: str,
        timeframe: str,
        scope: str,
        label: str,
        start_dt: datetime | None,
        end_dt: datetime | None,
    ) -> dict[str, Any]:
        start_dt = start_dt or ((end_dt or datetime.now(tz=timezone.utc)) - timedelta(hours=24))
        end_dt = end_dt or datetime.now(tz=timezone.utc)

        candles = self._load_candles(symbol, timeframe, start_dt, end_dt)
        strategy = create_strategy(strategy_name)
        strategy.initialize()
        broker = PaperBroker()
        risk_manager = RiskManager()

        warmup_bars = min(50, max(0, len(candles) - 1)) if len(candles) else 0
        stage_counts = Counter(
            {
                "candles_analyzed": 0,
                "setups_found": 0,
                "filters_passed": 0,
                "score_passed": 0,
                "risk_approved": 0,
                "orders_sent": 0,
                "orders_executed": 0,
                "trades_opened": 0,
                "trades_closed": 0,
            }
        )
        reasons = Counter()
        detailed_rows: list[dict[str, Any]] = []
        open_trade: dict[str, Any] | None = None
        highest_price = 0.0

        for i in range(warmup_bars, len(candles)):
            window = candles.iloc[: i + 1]
            enriched = strategy.calculate(window)
            last = enriched.iloc[-1]
            current_price = float(last["close"])
            timestamp = self._to_datetime(last.name)
            stage_counts["candles_analyzed"] += 1

            indicator_issue = self._indicator_issue(last, enriched)
            if indicator_issue is not None:
                reasons[indicator_issue] += 1
                detailed_rows.append(self._detail_row(scope, timestamp, current_price, "indicator_blocked", indicator_issue, None, None))
                continue

            if open_trade is not None:
                highest_price = max(highest_price, current_price)
                if self._trade_should_close(open_trade, current_price, highest_price):
                    stage_counts["trades_closed"] += 1
                    open_trade = None
                    highest_price = 0.0

            entry_signal = strategy.entry_signal(enriched)
            score = self._safe_score(strategy, enriched)
            setup_found = self._setup_detected(strategy, enriched, entry_signal)
            if not setup_found:
                reasons["mercado_sem_oportunidade"] += 1
                detailed_rows.append(self._detail_row(scope, timestamp, current_price, "no_setup", "mercado_sem_oportunidade", score, entry_signal.signal.value))
                continue

            stage_counts["setups_found"] += 1
            stage_counts["filters_passed"] += 1
            if score <= 0.0:
                reasons["score_insuficiente"] += 1
                detailed_rows.append(self._detail_row(scope, timestamp, current_price, "score_rejected", "score_insuficiente", score, entry_signal.signal.value))
                continue

            stage_counts["score_passed"] += 1
            if open_trade is not None:
                reasons["posicao_ja_aberta"] += 1
                detailed_rows.append(self._detail_row(scope, timestamp, current_price, "open_position_blocked", "posicao_ja_aberta", score, entry_signal.signal.value))
                continue

            try:
                risk_manager.evaluate_trade(
                    portfolio_value=broker.get_balance().cash,
                    entry_price=current_price,
                    stop_loss=entry_signal.stop_loss,
                    take_profit=entry_signal.take_profit,
                    trailing_stop_pct=entry_signal.trailing_stop_pct,
                    strategy_score=entry_signal.score,
                )
            except ValueError as exc:
                reason = self._normalize_risk_reason(str(exc))
                reasons[reason] += 1
                detailed_rows.append(self._detail_row(scope, timestamp, current_price, "risk_rejected", reason, score, entry_signal.signal.value))
                continue

            stage_counts["risk_approved"] += 1
            stage_counts["orders_sent"] += 1
            stage_counts["orders_executed"] += 1
            stage_counts["trades_opened"] += 1
            open_trade = {
                "entry_price": current_price,
                "entry_time": timestamp,
                "stop_loss": entry_signal.stop_loss,
                "take_profit": entry_signal.take_profit,
                "trailing_stop_pct": entry_signal.trailing_stop_pct,
            }
            highest_price = current_price
            detailed_rows.append(self._detail_row(scope, timestamp, current_price, "trade_opened", None, score, entry_signal.signal.value))

        if open_trade is not None:
            reasons["posicao_em_aberto"] += 1

        stage_rows = self._stage_rows(stage_counts)
        pipeline = {row["stage"]: row for row in stage_rows}
        rejections = self._reason_rows(reasons, int(stage_counts["candles_analyzed"]))
        return {
            "scope": scope,
            "label": label,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": f"{strategy_name}@{strategy_version}",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "candles_loaded": int(len(candles)),
            "warmup_bars": int(warmup_bars),
            "pipeline": pipeline,
            "stage_rows": stage_rows,
            "rejections": rejections,
            "details": detailed_rows[:2000],
            "open_positions_end": int(open_trade is not None),
            "summary": self._scope_summary(pipeline, rejections, open_trade is not None),
        }

    def _load_candles(self, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT open_time, open, high, low, close, volume
                    FROM candles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                      AND open_time >= :start_dt
                      AND open_time <= :end_dt
                    ORDER BY open_time ASC
                    """
                ),
                {"symbol": symbol, "timeframe": timeframe, "start_dt": start_dt, "end_dt": end_dt},
            ).fetchall()

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)

        return pd.DataFrame(
            [
                {
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
                for row in rows
            ],
            index=pd.DatetimeIndex([row[0] for row in rows], tz="UTC"),
        )

    def _build_heatmap_rows(self, strategy_name: str, strategy_version: str, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
        strategy_key = f"{strategy_name}@{strategy_version}"
        with get_session() as session:
            signal_rows = session.execute(
                text(
                    """
                    SELECT symbol, timeframe, COUNT(*) AS setups
                    FROM signal_snapshots
                    WHERE strategy = :strategy_key
                      AND timestamp >= :start_dt
                      AND timestamp <= :end_dt
                    GROUP BY symbol, timeframe
                    """
                ),
                {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt},
            ).mappings().all()

            trade_rows = session.execute(
                text(
                    """
                    SELECT symbol, timeframe,
                           COUNT(*) AS trades,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(pnl) AS net_profit,
                           SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
                           ABS(SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END)) AS gross_loss
                    FROM trade_history
                    WHERE strategy = :strategy_key
                      AND exit_time >= :start_dt
                      AND exit_time <= :end_dt
                    GROUP BY symbol, timeframe
                    """
                ),
                {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt},
            ).mappings().all()

        trade_map = {(str(row["symbol"]), str(row["timeframe"])): row for row in trade_rows}
        heatmap = []
        for row in signal_rows:
            key = (str(row["symbol"]), str(row["timeframe"]))
            trade_row = trade_map.get(key, {})
            trades = int(trade_row.get("trades") or 0)
            wins = int(trade_row.get("wins") or 0)
            gross_profit = float(trade_row.get("gross_profit") or 0.0)
            gross_loss = float(trade_row.get("gross_loss") or 0.0)
            net_profit = float(trade_row.get("net_profit") or 0.0)
            heatmap.append(
                {
                    "symbol": key[0],
                    "timeframe": key[1],
                    "setups": int(row["setups"] or 0),
                    "trades": trades,
                    "win_rate": round((wins / trades) * 100.0, 2) if trades else 0.0,
                    "profit_factor": round((gross_profit / gross_loss), 6) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
                    "expectancy": round((net_profit / trades), 6) if trades else 0.0,
                }
            )
        heatmap.sort(key=lambda row: (row["trades"], row["setups"]), reverse=True)
        return heatmap

    def _aggregate_rejections(self, scope_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        total_candles = sum(int(scope["pipeline"]["candles_analyzed"]["count"]) for scope in scope_results)
        reason_counter: Counter[str] = Counter()
        for scope in scope_results:
            for row in scope["rejections"]:
                reason_counter[str(row["reason"])] += int(row["count"])

        items = []
        for reason, count in reason_counter.most_common():
            items.append(
                {
                    "reason": reason,
                    "count": int(count),
                    "percent": self._percent(int(count), max(1, total_candles)),
                    "impact": int(count),
                    "impact_percent": self._percent(int(count), max(1, total_candles)),
                }
            )
        return items

    def _make_decision(self, scope_results: list[dict[str, Any]]) -> dict[str, Any]:
        populated = [scope for scope in scope_results if int(scope["pipeline"]["candles_analyzed"]["count"]) > 0]
        analysis_scope = max(
            populated or scope_results,
            key=lambda scope: int(scope["pipeline"]["candles_analyzed"]["count"]),
        )
        pipeline = analysis_scope["pipeline"]
        setups = int(pipeline["setups_found"]["count"])
        score_passed = int(pipeline["score_passed"]["count"])
        risk_approved = int(pipeline["risk_approved"]["count"])
        orders_sent = int(pipeline["orders_sent"]["count"])
        trades_opened = int(pipeline["trades_opened"]["count"])
        trades_closed = int(pipeline["trades_closed"]["count"])
        open_positions_end = int(analysis_scope["open_positions_end"])
        top_rejection = analysis_scope["rejections"][0]["reason"] if analysis_scope["rejections"] else "outro"

        if setups == 0:
            return {
                "option": "A",
                "decision": "A",
                "analysis_scope": analysis_scope["scope"],
                "summary": "A estrategia esta saudavel. O mercado apenas nao apresentou oportunidades.",
                "recommendation": "Nenhuma alteracao recomendada.",
                "bottleneck_stage": "setups_found",
                "bottleneck_reason": "mercado_sem_oportunidade",
            }

        if score_passed == 0:
            recommendation = f"{self._reason_percent(analysis_scope, 'score_insuficiente')} dos sinais morreram no score."
            return {
                "option": "B",
                "decision": "B",
                "analysis_scope": analysis_scope["scope"],
                "summary": "Existe gargalo operacional.",
                "recommendation": recommendation,
                "bottleneck_stage": "score_passed",
                "bottleneck_reason": "score_insuficiente",
            }

        if risk_approved == 0:
            recommendation = f"{self._reason_percent(analysis_scope, top_rejection)} dos sinais morreram no Risk Manager."
            return {
                "option": "B",
                "decision": "B",
                "analysis_scope": analysis_scope["scope"],
                "summary": "Existe gargalo operacional.",
                "recommendation": recommendation,
                "bottleneck_stage": "risk_approved",
                "bottleneck_reason": top_rejection,
            }

        if orders_sent == 0:
            return {
                "option": "B",
                "decision": "B",
                "analysis_scope": analysis_scope["scope"],
                "summary": "Existe gargalo operacional.",
                "recommendation": "As ordens nunca chegaram a ser enviadas para execucao.",
                "bottleneck_stage": "orders_sent",
                "bottleneck_reason": "execucao_bloqueada",
            }

        if trades_opened > 0 and (trades_closed == 0 or open_positions_end > 0):
            return {
                "option": "B",
                "decision": "B",
                "analysis_scope": analysis_scope["scope"],
                "summary": "Existe gargalo operacional.",
                "recommendation": "A estrategia abriu posicoes que nunca fecharam dentro da janela analisada.",
                "bottleneck_stage": "trades_closed",
                "bottleneck_reason": "posicao_em_aberto",
            }

        return {
            "option": "B",
            "decision": "B",
            "analysis_scope": analysis_scope["scope"],
            "summary": "Existe gargalo operacional.",
            "recommendation": "Houve oportunidades e a cadeia operacional nao converteu tudo em trades fechados.",
            "bottleneck_stage": self._bottleneck_stage(pipeline),
            "bottleneck_reason": top_rejection,
        }

    def _write_outputs(self, report: dict[str, Any], output_prefix: str, started_at: datetime) -> dict[str, str]:
        stamp = started_at.strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        csv_path = self._results_dir / f"{output_prefix}_{stamp}.csv"
        heatmap_path = self._results_dir / f"{output_prefix}_{stamp}.heatmap.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._render_markdown(report), encoding="utf-8")
        self._write_csv(csv_path, report)
        pd.DataFrame(report.get("heatmap", [])).to_csv(heatmap_path, index=False, encoding="utf-8-sig")

        return {
            "json": str(json_path),
            "markdown": str(md_path),
            "csv": str(csv_path),
            "heatmap_csv": str(heatmap_path),
        }

    def _write_csv(self, csv_path: Path, report: dict[str, Any]) -> None:
        rows = []
        for scope in report.get("scopes", []):
            for stage, values in scope.get("pipeline", {}).items():
                rows.append(
                    {
                        "scope": scope.get("scope"),
                        "label": scope.get("label"),
                        "stage": stage,
                        "count": values.get("count"),
                        "percent": values.get("percent"),
                        "impact": values.get("impact"),
                        "impact_percent": values.get("impact_percent"),
                    }
                )
        pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    def _render_markdown(self, report: dict[str, Any]) -> str:
        lines = [
            "# Strategy Diagnostics",
            "",
            f"Strategy: {report.get('strategy_name')}@{report.get('strategy_version')}",
            f"Symbol: {report.get('symbol')}",
            f"Timeframe: {report.get('timeframe')}",
            f"Source execution: {report.get('source_execution_id')}",
            f"Decision: OPCAO {report.get('decision', {}).get('option')}",
            f"Recommendation: {report.get('decision', {}).get('recommendation')}",
            "",
        ]

        for scope in report.get("scopes", []):
            lines.append(f"## {scope.get('label')}")
            lines.append("")
            lines.append("| Stage | Count | % of candles | Impact | Impact % |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            for stage_row in scope.get("stage_rows", []):
                lines.append(
                    f"| {stage_row.get('stage')} | {stage_row.get('count')} | {float(stage_row.get('percent') or 0.0):.2f} | {stage_row.get('impact')} | {float(stage_row.get('impact_percent') or 0.0):.2f} |"
                )
            lines.append("")
            lines.append("### Rejections")
            lines.append("")
            lines.append("| Reason | Count | % | Impact | Impact % |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            for row in scope.get("rejections", []):
                lines.append(
                    f"| {row.get('reason')} | {row.get('count')} | {float(row.get('percent') or 0.0):.2f} | {row.get('impact')} | {float(row.get('impact_percent') or 0.0):.2f} |"
                )
            lines.append("")

        lines.append("## Heatmap")
        lines.append("")
        lines.append("| Ativo | Timeframe | Setups | Trades | Win Rate | Profit Factor | Expectancy |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for row in report.get("heatmap", []):
            lines.append(
                f"| {row.get('symbol')} | {row.get('timeframe')} | {row.get('setups')} | {row.get('trades')} | {float(row.get('win_rate') or 0.0):.2f} | {float(row.get('profit_factor') or 0.0):.2f} | {float(row.get('expectancy') or 0.0):.6f} |"
            )
        lines.append("")
        lines.append("## Decision")
        lines.append("")
        lines.append(f"- Option: OPCAO {report.get('decision', {}).get('option')}")
        lines.append(f"- Summary: {report.get('decision', {}).get('summary')}")
        lines.append(f"- Bottleneck stage: {report.get('decision', {}).get('bottleneck_stage')}")
        lines.append(f"- Bottleneck reason: {report.get('decision', {}).get('bottleneck_reason')}")
        return "\n".join(lines)

    def _persist_checkpoint(
        self,
        *,
        diagnostic_execution_id: str,
        source_execution_id: str | None,
        report: dict[str, Any],
        outputs: dict[str, str],
        processed: int,
    ) -> None:
        with get_session() as session:
            history = HistoryPersistenceService(session)
            history.save_checkpoint(
                execution_id=diagnostic_execution_id,
                stage="strategy-diagnostics",
                processed=processed,
                completed=True,
                payload={
                    "source_execution_id": source_execution_id,
                    "decision": report.get("decision"),
                    "outputs": outputs,
                    "scopes": [
                        {
                            "scope": scope.get("scope"),
                            "candles_analyzed": scope.get("pipeline", {}).get("candles_analyzed", {}).get("count"),
                            "setups_found": scope.get("pipeline", {}).get("setups_found", {}).get("count"),
                            "score_passed": scope.get("pipeline", {}).get("score_passed", {}).get("count"),
                            "risk_approved": scope.get("pipeline", {}).get("risk_approved", {}).get("count"),
                            "trades_opened": scope.get("pipeline", {}).get("trades_opened", {}).get("count"),
                            "trades_closed": scope.get("pipeline", {}).get("trades_closed", {}).get("count"),
                        }
                        for scope in report.get("scopes", [])
                    ],
                },
            )

    @staticmethod
    def _setup_detected(strategy: BaseStrategy, enriched: pd.DataFrame, entry_signal: Any) -> bool:
        if hasattr(strategy, "event_entry_mask"):
            try:
                mask = strategy.event_entry_mask(enriched)
                if len(mask):
                    return bool(mask.iloc[-1])
            except Exception:
                pass
        return getattr(entry_signal, "signal", None) == SignalType.BUY

    @staticmethod
    def _safe_score(strategy: BaseStrategy, enriched: pd.DataFrame) -> float:
        try:
            return float(strategy.score(enriched))
        except Exception:
            return 0.0

    @staticmethod
    def _indicator_issue(last_row: pd.Series, enriched: pd.DataFrame) -> str | None:
        indicator_cols = [col for col in enriched.columns if col.lower() not in {"open", "high", "low", "close", "volume"}]
        if not indicator_cols:
            return None
        values = pd.to_numeric(last_row.reindex(indicator_cols), errors="coerce")
        if values.isna().any():
            return "indicadores_sem_aquecimento"
        numbers = values.astype(float).tolist()
        if not all(math.isfinite(number) for number in numbers):
            return "indicadores_sem_aquecimento"
        return None

    @staticmethod
    def _normalize_risk_reason(message: str) -> str:
        text_msg = message.lower()
        if "stop_loss" in text_msg or "must be below entry_price" in text_msg:
            return "stop_invalido"
        if "take_profit" in text_msg or "must be above entry_price" in text_msg:
            return "take_invalido"
        if "risk_reward" in text_msg or "reward" in text_msg:
            return "rr_insuficiente"
        if "capital" in text_msg or "portfolio" in text_msg or "cash" in text_msg:
            return "capital_insuficiente"
        if "exposure" in text_msg:
            return "exposicao_maxima"
        if "cooldown" in text_msg:
            return "cooldown"
        if "market" in text_msg and "closed" in text_msg:
            return "mercado_fechado"
        if "atr" in text_msg:
            return "atr_indisponivel"
        if "ema" in text_msg:
            return "ema_indisponivel"
        return "outro"

    @staticmethod
    def _trade_should_close(open_trade: dict[str, Any], current_price: float, highest_price: float) -> bool:
        stop_loss = open_trade.get("stop_loss")
        take_profit = open_trade.get("take_profit")
        trailing = open_trade.get("trailing_stop_pct")
        stop_hit = stop_loss is not None and current_price <= float(stop_loss)
        take_hit = take_profit is not None and current_price >= float(take_profit)
        trailing_hit = trailing is not None and current_price <= highest_price * (1.0 - float(trailing))
        return bool(stop_hit or take_hit or trailing_hit)

    @staticmethod
    def _detail_row(scope: str, timestamp: datetime, price: float, step: str, reason: str | None, score: float | None, signal: str | None) -> dict[str, Any]:
        return {
            "scope": scope,
            "timestamp": timestamp.isoformat(),
            "price": round(float(price), 10),
            "step": step,
            "reason": reason,
            "score": None if score is None else round(float(score), 6),
            "signal": signal,
        }

    @staticmethod
    def _stage_rows(stage_counts: Counter) -> list[dict[str, Any]]:
        ordered = [
            "candles_analyzed",
            "setups_found",
            "filters_passed",
            "score_passed",
            "risk_approved",
            "orders_sent",
            "orders_executed",
            "trades_opened",
            "trades_closed",
        ]
        rows: list[dict[str, Any]] = []
        previous = None
        base = max(1, int(stage_counts["candles_analyzed"]))
        for stage in ordered:
            current = int(stage_counts[stage])
            impact = 0 if previous is None else max(0, previous - current)
            rows.append(
                {
                    "stage": stage,
                    "count": current,
                    "percent": round((current / base) * 100.0, 2),
                    "impact": impact,
                    "impact_percent": round((impact / max(1, previous or base)) * 100.0, 2),
                }
            )
            previous = current
        return rows

    @staticmethod
    def _reason_rows(reasons: Counter, total: int) -> list[dict[str, Any]]:
        rows = []
        for reason, count in reasons.most_common():
            rows.append(
                {
                    "reason": reason,
                    "count": int(count),
                    "percent": round((int(count) / max(1, total)) * 100.0, 2),
                    "impact": int(count),
                    "impact_percent": round((int(count) / max(1, total)) * 100.0, 2),
                }
            )
        return rows

    @staticmethod
    def _scope_summary(pipeline: dict[str, Any], rejections: list[dict[str, Any]], open_position_end: bool) -> dict[str, Any]:
        return {
            "candle_to_setup_conversion": pipeline["setups_found"]["percent"],
            "setup_to_score_conversion": round((pipeline["score_passed"]["count"] / max(1, pipeline["setups_found"]["count"])) * 100.0, 2),
            "score_to_risk_conversion": round((pipeline["risk_approved"]["count"] / max(1, pipeline["score_passed"]["count"])) * 100.0, 2),
            "risk_to_trade_conversion": round((pipeline["trades_opened"]["count"] / max(1, pipeline["risk_approved"]["count"])) * 100.0, 2),
            "top_rejection": rejections[0] if rejections else None,
            "open_positions_end": int(bool(open_position_end)),
        }

    @staticmethod
    def _reason_percent(scope: dict[str, Any], reason: str) -> float:
        for row in scope.get("rejections", []):
            if str(row.get("reason")) == reason:
                return float(row.get("percent") or 0.0)
        return 0.0

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round((value / total) * 100.0, 2)

    @staticmethod
    def _to_datetime(value: Any) -> datetime:
        if value is None:
            return datetime.now(tz=timezone.utc)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if hasattr(value, "to_pydatetime"):
            ts = value.to_pydatetime()
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _bottleneck_stage(pipeline: dict[str, Any]) -> str:
        ordered = [
            "candles_analyzed",
            "setups_found",
            "filters_passed",
            "score_passed",
            "risk_approved",
            "orders_sent",
            "orders_executed",
            "trades_opened",
            "trades_closed",
        ]
        worst_stage = ordered[0]
        worst_drop = -1
        prev = None
        for stage in ordered:
            current = int(pipeline[stage]["count"])
            if prev is not None:
                drop = max(0, prev - current)
                if drop > worst_drop:
                    worst_drop = drop
                    worst_stage = stage
            prev = current
        return worst_stage

    def _load_state(self) -> dict[str, Any]:
        state_path = self._state_file
        if not state_path.exists():
            return {}
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _persist_checkpoint(
        self,
        *,
        diagnostic_execution_id: str,
        source_execution_id: str | None,
        report: dict[str, Any],
        outputs: dict[str, str],
        processed: int,
    ) -> None:
        with get_session() as session:
            history = HistoryPersistenceService(session)
            history.save_checkpoint(
                execution_id=diagnostic_execution_id,
                stage="strategy-diagnostics",
                processed=processed,
                completed=True,
                payload={
                    "source_execution_id": source_execution_id,
                    "decision": report.get("decision"),
                    "outputs": outputs,
                    "scopes": [
                        {
                            "scope": scope.get("scope"),
                            "candles_analyzed": scope.get("pipeline", {}).get("candles_analyzed", {}).get("count"),
                            "setups_found": scope.get("pipeline", {}).get("setups_found", {}).get("count"),
                            "score_passed": scope.get("pipeline", {}).get("score_passed", {}).get("count"),
                            "risk_approved": scope.get("pipeline", {}).get("risk_approved", {}).get("count"),
                            "trades_opened": scope.get("pipeline", {}).get("trades_opened", {}).get("count"),
                            "trades_closed": scope.get("pipeline", {}).get("trades_closed", {}).get("count"),
                        }
                        for scope in report.get("scopes", [])
                    ],
                },
            )
