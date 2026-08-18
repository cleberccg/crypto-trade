"""Official campaign orchestration for specialized ClassicDonchianBreakout paper trading."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import bindparam, text

from database.connection import get_session
from paper_trading.campaign_registry_store import upsert_campaign_registry_execution
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService
from utils.atomic_io import atomic_write_text
from paper_trading.edge_drift_monitor import (
    EdgeDriftContext,
    EdgeDriftMonitorConfig,
    EdgeDriftMonitorService,
    EdgeDriftThresholds,
)


@dataclass(frozen=True)
class SpecializedCampaignConfig:
    strategy_name: str = "ClassicDonchianBreakout"
    strategy_version: str = "v1.0"
    campaign_id: str | None = None
    specialized_report_file: str | None = None
    contexts: tuple[EdgeDriftContext, ...] = ()
    contexts_from_latest_report: bool = True
    monitor_lookback_days: int = 21
    monitor_history_window: int = 30
    phase1_min_days: int = 7
    phase1_min_trades: int = 50
    phase2_min_days: int = 21
    phase2_min_trades: int = 200
    min_profit_factor: float = 1.15
    min_expectancy: float = 0.0
    min_sharpe: float = 0.0
    max_drawdown: float = 0.20
    max_consecutive_critical_alerts: int = 2
    max_consecutive_non_normal_alerts: int = 3
    initial_capital: float = 10_000.0
    poll_seconds: float = 15.0
    bootstrap_bars: int = 1500
    bootstrap_replay_bars: int = 350
    max_cycles_per_context: int = 1
    min_trades_before_change: int = 0
    legacy_live_execution: bool = False
    ingest_execution_ids: tuple[str, ...] = ()
    hypothesis_config: dict[str, Any] | None = None
    output_prefix: str = "paper_specialized_campaign"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, (dict, list)) else None


def _latest_file(base_dir: Path, patterns: tuple[str, ...]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(p for p in sorted(base_dir.glob(pattern), key=lambda path: path.stat().st_mtime))
    return candidates[-1] if candidates else None


def _phase_complete(days_elapsed: int, trades: int, min_days: int, min_trades: int) -> bool:
    return days_elapsed >= int(min_days) and trades >= int(min_trades)


def _campaign_answer(p1_ok: bool, p2_ok: bool, killed: bool) -> str:
    if killed:
        return "NAO"
    if p1_ok and p2_ok:
        return "SIM"
    if p1_ok:
        return "PARCIALMENTE"
    return "NAO"


def _normalize_execution_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        output.append(token)
    return output


def _merge_execution_ids(existing: list[str], live_runs: list[dict[str, Any]]) -> list[str]:
    merged = list(existing)
    seen = set(merged)
    for run in live_runs:
        token = str((run or {}).get("execution_id") or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        merged.append(token)
    return merged


def _trade_is_after_marker(
    exit_time: datetime | None,
    trade_id: int,
    marker_exit_time: datetime | None,
    marker_trade_id: int,
) -> bool:
    if exit_time is None:
        return False
    if marker_exit_time is None:
        return True
    if exit_time > marker_exit_time:
        return True
    if exit_time < marker_exit_time:
        return False
    return int(trade_id) > int(marker_trade_id)


def _advance_trade_markers(
    marker_exit_time: datetime | None,
    marker_trade_id: int,
    rows: list[dict[str, Any]],
) -> tuple[datetime | None, int]:
    current_exit = marker_exit_time
    current_id = int(marker_trade_id)
    for row in rows:
        exit_time = _parse_dt(row.get("exit_time"))
        trade_id = int(row.get("id") or 0)
        if _trade_is_after_marker(exit_time, trade_id, current_exit, current_id):
            current_exit = exit_time
            current_id = trade_id
    return current_exit, current_id


def _parse_marker_trade_id(value: Any) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else 0
    except (TypeError, ValueError):
        return 0


class SpecializedPaperCampaignService:
    """Orchestrates the P1/P2 specialized paper campaign and its kill switch."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._campaign_registry_path = self._results_dir / "paper_specialized_campaign_registry.json"

    def run(self, cfg: SpecializedCampaignConfig) -> dict[str, Any]:
        state = self._load_state()
        started_at = _parse_dt(state.get("started_at")) if isinstance(state, dict) else None
        if started_at is None:
            started_at = datetime.now(tz=timezone.utc)
        last_eval_exit_time = _parse_dt(state.get("last_evaluated_exit_time")) if isinstance(state, dict) else None
        last_eval_trade_id = _parse_marker_trade_id(state.get("last_evaluated_trade_id") if isinstance(state, dict) else 0)
        campaign_id = str(cfg.campaign_id or (state.get("campaign_id") if isinstance(state, dict) else None) or f"spc-{uuid4().hex}")

        registry = self._load_campaign_registry()
        campaign_entry = registry.get(campaign_id, {}) if isinstance(registry.get(campaign_id), dict) else {}
        existing_execution_ids = _normalize_execution_ids(campaign_entry.get("execution_ids"))
        existing_execution_ids = _normalize_execution_ids(existing_execution_ids + _normalize_execution_ids(state.get("execution_ids") if isinstance(state, dict) else []))
        ingest_execution_ids = _normalize_execution_ids(list(cfg.ingest_execution_ids))

        reference_report = self._load_reference_report(cfg)
        contexts = list(cfg.contexts)
        if not contexts and cfg.contexts_from_latest_report:
            contexts = self._contexts_from_report(reference_report)
        if not contexts:
            contexts = self._contexts_from_matrix(cfg.strategy_name)
        if not contexts:
            raise RuntimeError("No approved contexts available for the specialized campaign.")

        live_runs: list[dict[str, Any]] = []
        if cfg.legacy_live_execution and cfg.max_cycles_per_context > 0:
            live_runs = self._run_live_rounds(cfg, contexts)

        execution_ids = _normalize_execution_ids(existing_execution_ids + ingest_execution_ids)
        execution_ids = _merge_execution_ids(execution_ids, live_runs)

        inferred_execution_ids = self._discover_execution_ids_for_campaign(
            strategy_name=cfg.strategy_name,
            strategy_version=cfg.strategy_version,
            contexts=contexts,
            started_at=started_at,
            marker_exit_time=last_eval_exit_time,
            marker_trade_id=last_eval_trade_id,
        )
        execution_ids = _normalize_execution_ids(execution_ids + inferred_execution_ids)

        incremental = self._discover_campaign_trades(
            strategy_name=cfg.strategy_name,
            strategy_version=cfg.strategy_version,
            contexts=contexts,
            execution_ids=execution_ids,
            marker_exit_time=last_eval_exit_time,
            marker_trade_id=last_eval_trade_id,
        )
        execution_ids = _normalize_execution_ids(execution_ids + incremental.get("execution_ids", []))

        self._upsert_campaign_registry_entry(
            campaign_id=campaign_id,
            strategy_name=cfg.strategy_name,
            strategy_version=cfg.strategy_version,
            execution_ids=execution_ids,
        )

        monitor_service = EdgeDriftMonitorService(self._base_dir)
        monitor_report = monitor_service.run(
            EdgeDriftMonitorConfig(
                strategy_name=cfg.strategy_name,
                strategy_version=cfg.strategy_version,
                campaign_id=campaign_id,
                specialized_report_file=cfg.specialized_report_file,
                contexts=tuple(contexts),
                contexts_from_latest_report=False,
                lookback_days=max(1, int(cfg.monitor_lookback_days)),
                history_window=max(3, int(cfg.monitor_history_window)),
                min_validation_days=max(1, int(cfg.phase1_min_days)),
                min_validation_trades=max(1, int(cfg.phase1_min_trades)),
                initial_capital=max(100.0, float(cfg.initial_capital)),
                thresholds=EdgeDriftThresholds(),
                output_prefix=f"{cfg.output_prefix}_monitor",
            )
        )

        metrics = self._collect_campaign_metrics(cfg.strategy_name, cfg.strategy_version, contexts, execution_ids, started_at)
        history = self._load_monitor_history()
        alert_state = self._alert_state(history)

        health_score_raw = (monitor_report.get("summary", {}) or {}).get("health_score")
        health_score = float(health_score_raw) if health_score_raw is not None else None
        alert_level = str((monitor_report.get("summary", {}) or {}).get("alert_level") or "NORMAL")
        metrics_ok = self._metrics_ok(metrics, cfg)
        scope_ok = int(metrics.get("outside_scope_trades") or 0) == 0
        p1_complete = _phase_complete(metrics["days_elapsed"], metrics["trades"], cfg.phase1_min_days, cfg.phase1_min_trades)
        p2_complete = _phase_complete(metrics["days_elapsed"], metrics["trades"], cfg.phase2_min_days, cfg.phase2_min_trades)

        p1_gate_ok = p1_complete and metrics_ok and scope_ok and alert_level == "NORMAL" and health_score is not None and health_score >= 70.0 and not alert_state["kill_switch"]
        p2_gate_ok = p2_complete and metrics_ok and scope_ok and alert_level == "NORMAL" and health_score is not None and health_score >= 80.0 and not alert_state["kill_switch"]
        kill_switch = self._kill_switch(metrics, monitor_report, alert_state, cfg)
        if kill_switch:
            p1_gate_ok = False
            p2_gate_ok = False

        current_phase = state.get("current_phase") if isinstance(state, dict) else None
        if current_phase not in {"P1", "P2", "COMPLETED", "STOPPED"}:
            current_phase = "P1"
        if current_phase == "STOPPED" and not kill_switch:
            current_phase = "P1"
        if kill_switch:
            current_phase = "STOPPED"
        elif p1_gate_ok and not p2_complete:
            current_phase = "P2"
        elif p2_gate_ok:
            current_phase = "COMPLETED"

        p1_approved = bool(p1_gate_ok)
        p2_approved = bool(p2_gate_ok)
        answer = _campaign_answer(p1_approved, p2_approved, kill_switch)
        final_status = "PAPER_APPROVED_SPECIALIZED" if answer == "SIM" else "PAPER_CANDIDATE"

        campaign_report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase": "PAPER_SPECIALIZED_CAMPAIGN",
            "strategy": {
                "name": cfg.strategy_name,
                "version": cfg.strategy_version,
                "classification_specialized": final_status,
            },
            "scope": {
                "campaign_id": campaign_id,
                "contexts": [asdict(ctx) for ctx in contexts],
                "context_count": len(contexts),
                "campaign_started_at": started_at.isoformat(),
                "execution_ids": execution_ids,
                "execution_source": "legacy_campaign_live" if cfg.legacy_live_execution else "external_paper_live",
            },
            "campaign_state": {
                "current_phase": current_phase,
                "phase1": {
                    "min_days": int(cfg.phase1_min_days),
                    "min_trades": int(cfg.phase1_min_trades),
                    "complete": p1_complete,
                    "approved": p1_approved,
                },
                "phase2": {
                    "min_days": int(cfg.phase2_min_days),
                    "min_trades": int(cfg.phase2_min_trades),
                    "complete": p2_complete,
                    "approved": p2_approved,
                },
                "campaign_days_elapsed": metrics["days_elapsed"],
                "campaign_trades": metrics["trades"],
                "kill_switch": kill_switch,
            },
            "monitor": monitor_report,
            "campaign_metrics": metrics,
            "live_runs": live_runs,
            "incremental": {
                "new_trades": int(incremental.get("new_trades") or 0),
                "new_scoped_trades": int(incremental.get("new_scoped_trades") or 0),
                "last_evaluated_exit_time": incremental.get("last_evaluated_exit_time"),
                "last_evaluated_trade_id": int(incremental.get("last_evaluated_trade_id") or 0),
            },
            "decision": {
                "answer": answer,
                "final_status": final_status,
                "should_continue": current_phase in {"P1", "P2"} and not kill_switch and answer != "SIM",
                "reason": self._decision_reason(answer, kill_switch, p1_approved, p2_approved, metrics_ok, scope_ok, alert_state),
                "promotion_blocked": not scope_ok,
                "promotion_blocked_reason": "Promotion blocked: Outside scope trades detected." if not scope_ok else None,
            },
            "kill_switch": {
                "triggered": kill_switch,
                "reasons": alert_state["reasons"],
            },
            "reports": {
                "daily": (monitor_report.get("report", {}) or {}).get("reports", {}).get("daily"),
                "weekly": (monitor_report.get("report", {}) or {}).get("reports", {}).get("weekly"),
                "consolidated": (monitor_report.get("report", {}) or {}).get("reports", {}).get("consolidated"),
                "edge_drift": monitor_report.get("outputs", {}),
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, campaign_report)
        state_payload = self._build_state_payload(state, campaign_report, outputs)
        self._write_state(state_payload)

        summary = {
            "strategy": cfg.strategy_name,
            "phase": current_phase,
            "answer": answer,
            "final_status": final_status,
            "should_continue": campaign_report["decision"]["should_continue"],
            "kill_switch": kill_switch,
            "health_score": health_score,
            "trades": metrics["trades"],
            "new_trades": int(incremental.get("new_trades") or 0),
            "days_elapsed": metrics["days_elapsed"],
            "outputs": outputs,
        }
        return {"summary": summary, "report": campaign_report, "outputs": outputs}

    def _discover_execution_ids_for_campaign(
        self,
        *,
        strategy_name: str,
        strategy_version: str,
        contexts: list[EdgeDriftContext],
        started_at: datetime,
        marker_exit_time: datetime | None,
        marker_trade_id: int,
    ) -> list[str]:
        strategy_key = f"{strategy_name}@{strategy_version}"
        stmt = text(
            """
            SELECT id, execution_id, symbol, timeframe, exit_time
            FROM trade_history
            WHERE strategy = :strategy_key
              AND exit_time IS NOT NULL
              AND exit_time >= :started_at
            ORDER BY exit_time ASC, id ASC
            """
        )
        with get_session() as session:
            rows = [dict(row) for row in session.execute(stmt, {"strategy_key": strategy_key, "started_at": started_at}).mappings().all()]

        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}
        seen: set[str] = set()
        execution_ids: list[str] = []
        for row in rows:
            ctx_key = (str(row.get("symbol")), str(row.get("timeframe")))
            if ctx_key not in context_set:
                continue
            trade_id = int(row.get("id") or 0)
            exit_time = _parse_dt(row.get("exit_time"))
            if not _trade_is_after_marker(exit_time, trade_id, marker_exit_time, marker_trade_id):
                continue
            execution_id = str(row.get("execution_id") or "").strip()
            if not execution_id or execution_id in seen:
                continue
            seen.add(execution_id)
            execution_ids.append(execution_id)
        return execution_ids

    def _discover_campaign_trades(
        self,
        *,
        strategy_name: str,
        strategy_version: str,
        contexts: list[EdgeDriftContext],
        execution_ids: list[str],
        marker_exit_time: datetime | None,
        marker_trade_id: int,
    ) -> dict[str, Any]:
        if not execution_ids:
            return {
                "execution_ids": [],
                "new_trades": 0,
                "new_scoped_trades": 0,
                "last_evaluated_exit_time": marker_exit_time.isoformat() if marker_exit_time else None,
                "last_evaluated_trade_id": int(marker_trade_id),
            }

        strategy_key = f"{strategy_name}@{strategy_version}"
        stmt = (
            text(
                """
                SELECT id, execution_id, symbol, timeframe, exit_time
                FROM trade_history
                WHERE strategy = :strategy_key
                  AND exit_time IS NOT NULL
                  AND execution_id IN :execution_ids
                ORDER BY exit_time ASC, id ASC
                """
            )
            .bindparams(bindparam("execution_ids", expanding=True))
        )
        with get_session() as session:
            rows = [dict(row) for row in session.execute(stmt, {"strategy_key": strategy_key, "execution_ids": execution_ids}).mappings().all()]

        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}
        scoped_new_rows: list[dict[str, Any]] = []
        seen_exec: set[str] = set(execution_ids)
        for row in rows:
            execution_id = str(row.get("execution_id") or "").strip()
            if execution_id:
                seen_exec.add(execution_id)
            ctx_key = (str(row.get("symbol")), str(row.get("timeframe")))
            if ctx_key not in context_set:
                continue
            trade_id = int(row.get("id") or 0)
            exit_time = _parse_dt(row.get("exit_time"))
            if _trade_is_after_marker(exit_time, trade_id, marker_exit_time, marker_trade_id):
                scoped_new_rows.append(row)

        new_exit_time, new_trade_id = _advance_trade_markers(marker_exit_time, marker_trade_id, scoped_new_rows)
        return {
            "execution_ids": sorted(seen_exec),
            "new_trades": len(scoped_new_rows),
            "new_scoped_trades": len(scoped_new_rows),
            "last_evaluated_exit_time": new_exit_time.isoformat() if new_exit_time else None,
            "last_evaluated_trade_id": int(new_trade_id),
        }

    def _run_live_rounds(self, cfg: SpecializedCampaignConfig, contexts: list[EdgeDriftContext]) -> list[dict[str, Any]]:
        service = PaperLiveService(self._base_dir)
        runs: list[dict[str, Any]] = []
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
                    max_cycles=max(1, int(cfg.max_cycles_per_context)),
                    resume=True,
                    min_trades_before_change=max(0, int(cfg.min_trades_before_change)),
                    hypothesis_config=cfg.hypothesis_config,
                    output_prefix=f"{cfg.output_prefix}_{self._slug(ctx.symbol)}_{ctx.timeframe}",
                )
            )
            runs.append(
                {
                    "symbol": ctx.symbol,
                    "timeframe": ctx.timeframe,
                    "status": run.get("status"),
                    "execution_id": run.get("execution_id"),
                    "closed_trades": run.get("closed_trades"),
                    "processed_bars": run.get("processed_bars"),
                }
            )
        return runs

    def _load_reference_report(self, cfg: SpecializedCampaignConfig) -> dict[str, Any]:
        if cfg.specialized_report_file:
            report_path = Path(cfg.specialized_report_file)
        else:
            report_path = self._latest_report_file("paper_specialized_validation_")

        if report_path is not None and report_path.exists():
            data = _load_json(report_path)
            if isinstance(data, dict):
                data["source_report_file"] = str(report_path)
                return data

        for fallback_pattern in (
            "edge_external_validation_lab_*.json",
            "edge_operational_pipeline_*.json",
            "edge_discovery_lab_*.json",
        ):
            report_path = _latest_file(self._results_dir, (fallback_pattern,))
            if report_path is None or not report_path.exists():
                continue
            data = _load_json(report_path)
            if isinstance(data, dict):
                data["source_report_file"] = str(report_path)
                return data

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "strategy": {"name": cfg.strategy_name, "version": cfg.strategy_version},
            "source_report_file": None,
        }

    def _contexts_from_report(self, report: dict[str, Any]) -> list[EdgeDriftContext]:
        scope = report.get("scope", {}) if isinstance(report.get("scope"), dict) else {}
        rows = scope.get("contexts", []) if isinstance(scope.get("contexts"), list) else []
        contexts: list[EdgeDriftContext] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            if symbol and timeframe:
                contexts.append(EdgeDriftContext(symbol=symbol, timeframe=timeframe))
        return contexts

    def _contexts_from_matrix(self, strategy_name: str) -> list[EdgeDriftContext]:
        matrix_file = _latest_file(self._results_dir, ("edge_discovery_lab_*_matrix.csv",))
        if matrix_file is None or not matrix_file.exists():
            return []

        import csv

        contexts: list[EdgeDriftContext] = []
        seen: set[tuple[str, str]] = set()
        with matrix_file.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [row for row in reader if isinstance(row, dict)]

        candidate_rows = [
            row
            for row in rows
            if str(row.get("strategy") or "") == strategy_name
            and str(row.get("platform_strategy_name") or "") == strategy_name
            and str(row.get("regime_type") or "") == "full"
            and str(row.get("status") or "") == "ok"
        ]

        candidate_rows.sort(
            key=lambda row: (
                -float(row.get("profit_factor") or 0.0),
                -float(row.get("number_of_trades") or 0.0),
                -float(row.get("expectancy") or 0.0),
            )
        )

        for row in candidate_rows:
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            key = (symbol, timeframe)
            if not symbol or not timeframe or key in seen:
                continue
            seen.add(key)
            contexts.append(EdgeDriftContext(symbol=symbol, timeframe=timeframe))
        return contexts

    def _collect_campaign_metrics(
        self,
        strategy_name: str,
        strategy_version: str,
        contexts: list[EdgeDriftContext],
        execution_ids: list[str],
        started_at: datetime,
    ) -> dict[str, Any]:
        if not execution_ids:
            return {
                "trades": 0,
                "days_elapsed": max(1, (datetime.now(tz=timezone.utc) - started_at).days + 1),
                "net_profit": 0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "expectancy": 0.0,
                "drawdown": 0.0,
                "win_rate": 0.0,
                "first_trade_at": None,
                "last_trade_at": None,
                "scoped_trades": 0,
                "outside_scope_trades": 0,
                "campaign_execution_ids": [],
            }

        strategy_key = f"{strategy_name}@{strategy_version}"
        rows: list[dict[str, Any]] = []
        stmt = (
            text(
                """
                SELECT execution_id, symbol, timeframe, entry_time, exit_time, pnl, pnl_percent
                FROM trade_history
                WHERE strategy = :strategy_key
                  AND exit_time IS NOT NULL
                  AND execution_id IN :execution_ids
                ORDER BY exit_time ASC
                """
            )
            .bindparams(bindparam("execution_ids", expanding=True))
        )
        with get_session() as session:
            result = session.execute(stmt, {"strategy_key": strategy_key, "execution_ids": execution_ids}).mappings().all()
            rows = [dict(row) for row in result]

        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}
        scoped = [row for row in rows if (str(row.get("symbol")), str(row.get("timeframe"))) in context_set]
        pnl = [float(row.get("pnl") or 0.0) for row in scoped]
        pnl_pct = [float(row.get("pnl_percent") or 0.0) for row in scoped]
        wins = [value for value in pnl if value > 0.0]
        losses = [value for value in pnl if value <= 0.0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0.0 else (999.0 if gross_profit > 0.0 else 0.0)
        net_profit = sum(pnl)
        trades = len(scoped)
        expectancy = (net_profit / trades) if trades else 0.0
        win_rate = (len(wins) / trades) if trades else 0.0

        sharpe = 0.0
        if len(pnl_pct) > 1:
            mean_ret = sum(pnl_pct) / len(pnl_pct)
            variance = sum((value - mean_ret) ** 2 for value in pnl_pct) / (len(pnl_pct) - 1)
            std_dev = variance ** 0.5
            if std_dev > 0.0:
                sharpe = mean_ret / std_dev

        return {
            "trades": trades,
            "days_elapsed": max(1, (datetime.now(tz=timezone.utc) - started_at).days + 1),
            "net_profit": round(net_profit, 6),
            "profit_factor": round(profit_factor, 6),
            "sharpe": round(sharpe, 6),
            "expectancy": round(expectancy, 6),
            "drawdown": round(self._max_drawdown(pnl_pct), 6),
            "win_rate": round(win_rate, 6),
            "first_trade_at": self._first_trade_at(scoped),
            "last_trade_at": self._last_trade_at(scoped),
            "scoped_trades": trades,
            "outside_scope_trades": len(rows) - len(scoped),
            "campaign_execution_ids": execution_ids,
        }

    def _metrics_ok(self, metrics: dict[str, Any], cfg: SpecializedCampaignConfig) -> bool:
        return (
            float(metrics.get("profit_factor") or 0.0) >= float(cfg.min_profit_factor)
            and float(metrics.get("expectancy") or 0.0) > float(cfg.min_expectancy)
            and float(metrics.get("sharpe") or 0.0) > float(cfg.min_sharpe)
            and float(metrics.get("drawdown") or 0.0) <= float(cfg.max_drawdown)
        )

    def _kill_switch(
        self,
        metrics: dict[str, Any],
        monitor_report: dict[str, Any],
        alert_state: dict[str, Any],
        cfg: SpecializedCampaignConfig,
    ) -> bool:
        summary = monitor_report.get("summary", {}) if isinstance(monitor_report.get("summary"), dict) else {}
        health_score_raw = summary.get("health_score")
        health_score = float(health_score_raw) if health_score_raw is not None else None
        alert_level = str(summary.get("alert_level") or "NORMAL")
        if alert_level == "INSUFFICIENT_REFERENCE" or health_score is None:
            return False
        metrics_ok = self._metrics_ok(metrics, cfg)

        if alert_level == "CRITICO":
            return True
        if health_score < 50.0:
            return True
        if not metrics_ok:
            return True
        if alert_state["critical_streak"] >= int(cfg.max_consecutive_critical_alerts):
            return True
        if alert_state["non_normal_streak"] >= int(cfg.max_consecutive_non_normal_alerts):
            return True
        if alert_state["persistent_divergence"]:
            return True
        return False

    def _alert_state(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        if not history:
            return {
                "critical_streak": 0,
                "non_normal_streak": 0,
                "persistent_divergence": False,
                "kill_switch": False,
                "reasons": [],
            }

        tail = history[-5:]
        critical_streak = 0
        non_normal_streak = 0
        reasons: list[str] = []
        for row in reversed(tail):
            alert = str(row.get("alert_level") or row.get("alert") or "NORMAL")
            if alert == "CRITICO":
                critical_streak += 1
                non_normal_streak += 1
                reasons.append("critical_monitor_alert")
            elif alert == "ATENCAO":
                non_normal_streak += 1
                reasons.append("attention_monitor_alert")
            else:
                break

        recent = [str(row.get("alert_level") or row.get("alert") or "NORMAL") for row in tail[-3:]]
        persistent_divergence = len(recent) == 3 and all(alert in {"ATENCAO", "CRITICO"} for alert in recent)
        if persistent_divergence:
            reasons.append("persistent_divergence_from_reference")

        return {
            "critical_streak": critical_streak,
            "non_normal_streak": non_normal_streak,
            "persistent_divergence": persistent_divergence,
            "kill_switch": critical_streak >= 2 or persistent_divergence,
            "reasons": sorted(set(reasons)),
        }

    def _decision_reason(
        self,
        answer: str,
        killed: bool,
        p1_ok: bool,
        p2_ok: bool,
        metrics_ok: bool,
        scope_ok: bool,
        alert_state: dict[str, Any],
    ) -> str:
        if not scope_ok:
            return "Promotion blocked: Outside scope trades detected."
        if killed:
            return "Kill switch acionado por degradação consistente ou divergencia persistente."
        if answer == "SIM":
            return "P1 e P2 cumpridos com estabilidade operacional adequada."
        if p1_ok and not p2_ok:
            return "P1 aprovado; manter campanha em extensao para atingir P2."
        if not metrics_ok:
            return "Critérios quantitativos ainda não atendidos."
        if alert_state.get("persistent_divergence"):
            return "Divergencia persistente em relação ao baseline monitorado."
        return "Campanha ainda em monitoramento dentro do escopo aprovado."

    def _build_state_payload(self, previous: dict[str, Any] | None, report: dict[str, Any], outputs: dict[str, str]) -> dict[str, Any]:
        monitor_summary = report.get("monitor", {}).get("summary", {}) if isinstance(report.get("monitor"), dict) else {}
        previous_started_at = previous.get("started_at") if isinstance(previous, dict) else None
        scope = report.get("scope", {}) if isinstance(report.get("scope"), dict) else {}
        incremental = report.get("incremental", {}) if isinstance(report.get("incremental"), dict) else {}
        return {
            "campaign_id": scope.get("campaign_id"),
            "execution_ids": _normalize_execution_ids(scope.get("execution_ids")),
            "started_at": previous_started_at or report.get("scope", {}).get("campaign_started_at"),
            "last_evaluated_exit_time": incremental.get("last_evaluated_exit_time"),
            "last_evaluated_trade_id": int(incremental.get("last_evaluated_trade_id") or 0),
            "updated_at": report.get("generated_at"),
            "current_phase": report.get("campaign_state", {}).get("current_phase"),
            "last_answer": report.get("decision", {}).get("answer"),
            "last_status": report.get("decision", {}).get("final_status"),
            "last_health_score": monitor_summary.get("health_score"),
            "last_alert_level": monitor_summary.get("alert_level"),
            "kill_switch": report.get("kill_switch", {}).get("triggered"),
            "kill_switch_reasons": report.get("kill_switch", {}).get("reasons", []),
            "phase1": report.get("campaign_state", {}).get("phase1"),
            "phase2": report.get("campaign_state", {}).get("phase2"),
            "outputs": outputs,
        }

    def _load_monitor_history(self) -> list[dict[str, Any]]:
        path = self._results_dir / "edge_drift_monitor_history.json"
        data = _load_json(path)
        return data if isinstance(data, list) else []

    def _load_state(self) -> dict[str, Any]:
        path = self._results_dir / "paper_specialized_campaign_state.json"
        data = _load_json(path)
        if data is None:
            backup = path.with_suffix(path.suffix + ".bak")
            data = _load_json(backup)
        return data if isinstance(data, dict) else {}

    def _load_campaign_registry(self) -> dict[str, Any]:
        data = _load_json(self._campaign_registry_path)
        if not isinstance(data, dict):
            return {}
        campaigns = data.get("campaigns") if isinstance(data.get("campaigns"), dict) else {}
        return dict(campaigns)

    def _upsert_campaign_registry_entry(
        self,
        *,
        campaign_id: str,
        strategy_name: str,
        strategy_version: str,
        execution_ids: list[str],
    ) -> None:
        upsert_campaign_registry_execution(
            registry_path=self._campaign_registry_path,
            campaign_id=campaign_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            execution_ids=_normalize_execution_ids(execution_ids),
        )

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self._results_dir / "paper_specialized_campaign_state.json"
        payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
        atomic_write_text(path, payload, encoding="utf-8")
        atomic_write_text(path.with_suffix(path.suffix + ".bak"), payload, encoding="utf-8")

    def _write_outputs(self, output_prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        return {"json": str(json_path), "md": str(md_path), "state": str(self._results_dir / "paper_specialized_campaign_state.json")}

    def _latest_report_file(self, prefix: str) -> Path | None:
        files = sorted(self._results_dir.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1] if files else None

    @staticmethod
    def _slug(value: str) -> str:
        return str(value).strip().replace("/", "_").replace(" ", "_")

    @staticmethod
    def _max_drawdown(pnl_pct: list[float]) -> float:
        if not pnl_pct:
            return 0.0
        cumulative = 0.0
        equity = [0.0]
        for value in pnl_pct:
            cumulative += value
            equity.append(cumulative)
        peak = equity[0]
        max_dd = 0.0
        for value in equity:
            if value > peak:
                peak = value
            if peak > 0.0:
                drawdown = (peak - value) / peak
                if drawdown > max_dd:
                    max_dd = drawdown
        return max_dd

    @staticmethod
    def _first_trade_at(rows: list[dict[str, Any]]) -> str | None:
        times = [_parse_dt(row.get("entry_time")) for row in rows]
        times = [item for item in times if item is not None]
        return min(times).isoformat() if times else None

    @staticmethod
    def _last_trade_at(rows: list[dict[str, Any]]) -> str | None:
        times = [_parse_dt(row.get("exit_time")) for row in rows]
        times = [item for item in times if item is not None]
        return max(times).isoformat() if times else None

    def _to_markdown(self, report: dict[str, Any]) -> str:
        state = report.get("campaign_state", {}) if isinstance(report.get("campaign_state"), dict) else {}
        decision = report.get("decision", {}) if isinstance(report.get("decision"), dict) else {}
        monitor_summary = (report.get("monitor", {}) or {}).get("summary", {}) if isinstance(report.get("monitor"), dict) else {}

        lines = [
            "# Specialized Paper Campaign",
            "",
            f"- Strategy: {(report.get('strategy') or {}).get('name')}",
            f"- Version: {(report.get('strategy') or {}).get('version')}",
            f"- Answer: {decision.get('answer')}",
            f"- Final status: {decision.get('final_status')}",
            f"- Current phase: {state.get('current_phase')}",
            f"- Kill switch: {state.get('kill_switch')}",
            "",
            "## Phase Gate",
            f"- P1 complete: {state.get('phase1', {}).get('complete') if isinstance(state.get('phase1'), dict) else None}",
            f"- P1 approved: {state.get('phase1', {}).get('approved') if isinstance(state.get('phase1'), dict) else None}",
            f"- P2 complete: {state.get('phase2', {}).get('complete') if isinstance(state.get('phase2'), dict) else None}",
            f"- P2 approved: {state.get('phase2', {}).get('approved') if isinstance(state.get('phase2'), dict) else None}",
            "",
            "## Monitor",
            f"- Health score: {monitor_summary.get('health_score')}",
            f"- Alert level: {monitor_summary.get('alert_level')}",
            f"- Trades: {monitor_summary.get('trades')}",
        ]

        campaign_metrics = report.get("campaign_metrics", {}) if isinstance(report.get("campaign_metrics"), dict) else {}
        lines.extend([
            "",
            "## Campaign Metrics",
            f"- Days elapsed: {campaign_metrics.get('days_elapsed')}",
            f"- Trades: {campaign_metrics.get('trades')}",
            f"- Profit factor: {campaign_metrics.get('profit_factor')}",
            f"- Sharpe: {campaign_metrics.get('sharpe')}",
            f"- Expectancy: {campaign_metrics.get('expectancy')}",
            f"- Drawdown: {campaign_metrics.get('drawdown')}",
            f"- Win rate: {campaign_metrics.get('win_rate')}",
        ])
        return "\n".join(lines) + "\n"
