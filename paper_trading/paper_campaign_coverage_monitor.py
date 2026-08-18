"""Operational coverage monitor for specialized paper campaign contexts."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text

from database.connection import get_session
from paper_trading.edge_drift_monitor import EdgeDriftContext


@dataclass(frozen=True)
class PaperCampaignCoverageConfig:
    campaign_id: str
    strategy_name: str = "ClassicDonchianBreakout"
    strategy_version: str = "v1.0"
    stale_minutes: int = 180
    min_coverage_percent: float = 90.0
    critical_coverage_percent: float = 75.0
    output_prefix: str = "paper_specialized_campaign_coverage"


@dataclass(frozen=True)
class ContextCoverageSnapshot:
    symbol: str
    timeframe: str
    execution_id: str | None
    status: str
    last_activity: str | None
    minutes_without_activity: int | None
    trades: int
    last_trade: str | None
    last_signal: str | None
    last_cycle_executed: int | None
    last_candle_processed: str | None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _max_dt(*values: datetime | None) -> datetime | None:
    valid = [value for value in values if isinstance(value, datetime)]
    return max(valid) if valid else None


def _coverage_percent(active: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((float(active) / float(total)) * 100.0, 6)


def _format_minutes(value: int | None) -> str:
    if value is None:
        return "N/A"
    minutes = max(0, int(value))
    hours, rem = divmod(minutes, 60)
    days, rem_hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d{rem_hours}h{rem}m"
    if hours > 0:
        return f"{hours}h{rem}m"
    return f"{rem}m"


def _status_for_context(
    *,
    last_activity: datetime | None,
    stale_minutes: int,
    has_process_reference: bool,
    has_unparseable_activity: bool,
    now: datetime,
) -> tuple[str, int | None]:
    if last_activity is not None:
        minutes_without_activity = max(0, int((now - last_activity).total_seconds() // 60))
        if minutes_without_activity <= max(1, int(stale_minutes)):
            return "ACTIVE", minutes_without_activity
        return "STALE", minutes_without_activity

    if has_unparseable_activity:
        return "UNKNOWN", None

    if has_process_reference:
        return "STOPPED", None

    return "STOPPED", None


class PaperCampaignCoverageService:
    """Read-only monitor that checks execution coverage of approved campaign contexts."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._campaign_registry_path = self._results_dir / "paper_specialized_campaign_registry.json"

    def run(self, cfg: PaperCampaignCoverageConfig) -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        contexts = self._resolve_contexts(cfg)
        if not contexts:
            raise RuntimeError("No approved contexts found for campaign coverage monitoring.")

        execution_ids = self._resolve_execution_ids(cfg)
        states = self._load_paper_live_states(cfg, execution_ids)
        trade_activity = self._fetch_trade_activity(cfg, contexts, execution_ids)
        signal_activity = self._fetch_signal_activity(cfg, contexts, execution_ids)
        execution_activity = self._fetch_execution_activity(execution_ids)

        rows: list[ContextCoverageSnapshot] = []
        coverage_gaps: list[dict[str, Any]] = []
        context_alerts: list[dict[str, Any]] = []

        for ctx in contexts:
            key = (ctx.symbol, ctx.timeframe)
            context_states = states.get(key, [])
            trade_data = trade_activity.get(key, {})
            signal_data = signal_activity.get(key, {})

            active_execution_id = self._resolve_active_execution_id(
                context_states=context_states,
                trade_data=trade_data,
                signal_data=signal_data,
            )
            execution_meta = execution_activity.get(active_execution_id or "", {})
            latest_state = self._pick_latest_state(context_states, active_execution_id)

            last_trade_dt = _parse_dt(trade_data.get("last_trade"))
            last_signal_dt = _parse_dt(signal_data.get("last_signal"))
            last_candle_dt = _parse_dt((latest_state or {}).get("last_open_time"))

            # Heartbeat de estado (updated_at) nao conta como progresso operacional.
            last_activity_dt = _max_dt(last_trade_dt, last_signal_dt, last_candle_dt)
            has_process_reference = bool(latest_state or execution_meta or active_execution_id)
            has_unparseable_activity = (
                bool(trade_data.get("trades"))
                and last_trade_dt is None
            ) or (
                bool(signal_data.get("signals"))
                and last_signal_dt is None
            )
            status, minutes_without_activity = _status_for_context(
                last_activity=last_activity_dt,
                stale_minutes=max(1, int(cfg.stale_minutes)),
                has_process_reference=has_process_reference,
                has_unparseable_activity=has_unparseable_activity,
                now=now,
            )

            if status in {"ACTIVE", "STALE", "STOPPED"}:
                context_alerts.append(
                    {
                        "type": status,
                        "symbol": ctx.symbol,
                        "timeframe": ctx.timeframe,
                        "execution_id": active_execution_id,
                        "minutes_without_activity": minutes_without_activity,
                    }
                )

            if status != "ACTIVE":
                coverage_gaps.append(
                    {
                        "symbol": ctx.symbol,
                        "timeframe": ctx.timeframe,
                        "status": status,
                        "minutes_without_activity": minutes_without_activity,
                    }
                )

            rows.append(
                ContextCoverageSnapshot(
                    symbol=ctx.symbol,
                    timeframe=ctx.timeframe,
                    execution_id=active_execution_id,
                    status=status,
                    last_activity=last_activity_dt.isoformat() if last_activity_dt else None,
                    minutes_without_activity=minutes_without_activity,
                    trades=int(trade_data.get("trades") or 0),
                    last_trade=last_trade_dt.isoformat() if last_trade_dt else None,
                    last_signal=last_signal_dt.isoformat() if last_signal_dt else None,
                    last_cycle_executed=self._coerce_int((latest_state or {}).get("cycles")),
                    last_candle_processed=last_candle_dt.isoformat() if last_candle_dt else None,
                )
            )

        approved_contexts = len(rows)
        active_contexts = len([row for row in rows if row.status == "ACTIVE"])
        stale_contexts = len([row for row in rows if row.status == "STALE"])
        stopped_contexts = len([row for row in rows if row.status == "STOPPED"])
        unknown_contexts = len([row for row in rows if row.status == "UNKNOWN"])
        coverage_percent = _coverage_percent(active_contexts, approved_contexts)
        coverage_ok = coverage_percent >= float(cfg.min_coverage_percent)

        gap_candidates = [row for row in rows if row.minutes_without_activity is not None]
        largest_gap = max(gap_candidates, key=lambda row: int(row.minutes_without_activity or 0)) if gap_candidates else None

        global_alerts: list[dict[str, Any]] = []
        if coverage_percent < float(cfg.critical_coverage_percent):
            global_alerts.append(
                {
                    "type": "CRITICAL_COVERAGE",
                    "coverage_percent": coverage_percent,
                    "threshold": float(cfg.critical_coverage_percent),
                }
            )
        elif coverage_percent < float(cfg.min_coverage_percent):
            global_alerts.append(
                {
                    "type": "LOW_COVERAGE",
                    "coverage_percent": coverage_percent,
                    "threshold": float(cfg.min_coverage_percent),
                }
            )

        report = {
            "generated_at": now.isoformat(),
            "phase": "PAPER_SPECIALIZED_CAMPAIGN_COVERAGE",
            "strategy": {
                "name": cfg.strategy_name,
                "version": cfg.strategy_version,
            },
            "scope": {
                "campaign_id": cfg.campaign_id,
                "approved_contexts": [asdict(ctx) for ctx in contexts],
                "approved_contexts_count": approved_contexts,
                "execution_ids": execution_ids,
                "execution_ids_count": len(execution_ids),
                "data_sources": {
                    "trade_history": "trade_history",
                    "signal_history": "signal_snapshots",
                    "execution_history": "execution_sessions",
                },
            },
            "summary": {
                "approved_contexts": approved_contexts,
                "active_contexts": active_contexts,
                "stale_contexts": stale_contexts,
                "stopped_contexts": stopped_contexts,
                "unknown_contexts": unknown_contexts,
                "coverage_percent": coverage_percent,
                "coverage_ok": coverage_ok,
                "coverage_sufficient": "SIM" if coverage_ok else "NAO",
                "largest_gap": {
                    "symbol": largest_gap.symbol if largest_gap else None,
                    "timeframe": largest_gap.timeframe if largest_gap else None,
                    "minutes_without_activity": largest_gap.minutes_without_activity if largest_gap else None,
                    "formatted": _format_minutes(largest_gap.minutes_without_activity if largest_gap else None),
                },
            },
            "coverage": {
                "coverage_percent": coverage_percent,
                "coverage_ok": coverage_ok,
                "coverage_gaps": coverage_gaps,
                "formula": f"{active_contexts}/{approved_contexts}",
            },
            "thresholds": {
                "stale_minutes": max(1, int(cfg.stale_minutes)),
                "min_coverage_percent": float(cfg.min_coverage_percent),
                "critical_coverage_percent": float(cfg.critical_coverage_percent),
            },
            "contexts": [asdict(row) for row in rows],
            "alerts": {
                "context_alerts": context_alerts,
                "global_alerts": global_alerts,
                "all": [*context_alerts, *global_alerts],
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        summary = {
            "campaign_id": cfg.campaign_id,
            "strategy": cfg.strategy_name,
            "version": cfg.strategy_version,
            "approved_contexts": approved_contexts,
            "active_contexts": active_contexts,
            "coverage_percent": coverage_percent,
            "coverage_ok": coverage_ok,
            "coverage_sufficient": "SIM" if coverage_ok else "NAO",
            "largest_gap_context": f"{largest_gap.symbol} {largest_gap.timeframe}" if largest_gap else None,
            "largest_gap_minutes": largest_gap.minutes_without_activity if largest_gap else None,
            "largest_gap_formatted": _format_minutes(largest_gap.minutes_without_activity if largest_gap else None),
            "outputs": outputs,
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _resolve_contexts(self, cfg: PaperCampaignCoverageConfig) -> list[EdgeDriftContext]:
        contexts = self._contexts_from_campaign_report(cfg)
        if contexts:
            return contexts
        return self._contexts_from_matrix(cfg.strategy_name)

    def _contexts_from_campaign_report(self, cfg: PaperCampaignCoverageConfig) -> list[EdgeDriftContext]:
        reports = sorted(self._results_dir.glob("paper_specialized_campaign_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in reports:
            payload = self._load_json(path)
            if not isinstance(payload, dict):
                continue
            scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
            strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
            if str(scope.get("campaign_id") or "") != str(cfg.campaign_id):
                continue
            strategy_name = str(strategy.get("name") or "")
            strategy_version = str(strategy.get("version") or "")
            if strategy_name and strategy_name != cfg.strategy_name:
                continue
            if strategy_version and strategy_version != cfg.strategy_version:
                continue

            context_rows = scope.get("contexts") if isinstance(scope.get("contexts"), list) else []
            contexts: list[EdgeDriftContext] = []
            seen: set[tuple[str, str]] = set()
            for row in context_rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").strip()
                timeframe = str(row.get("timeframe") or "").strip()
                key = (symbol, timeframe)
                if not symbol or not timeframe or key in seen:
                    continue
                seen.add(key)
                contexts.append(EdgeDriftContext(symbol=symbol, timeframe=timeframe))
            if contexts:
                return contexts
        return []

    def _contexts_from_matrix(self, strategy_name: str) -> list[EdgeDriftContext]:
        matrix_files = sorted(self._results_dir.glob("edge_discovery_lab_*_matrix.csv"), key=lambda path: path.stat().st_mtime)
        if not matrix_files:
            return []

        matrix_file = matrix_files[-1]
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
                -self._coerce_float(row.get("profit_factor")),
                -self._coerce_float(row.get("number_of_trades")),
                -self._coerce_float(row.get("expectancy")),
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

    def _resolve_execution_ids(self, cfg: PaperCampaignCoverageConfig) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def _ingest(values: list[Any]) -> None:
            for value in values:
                token = str(value or "").strip()
                if not token or token in seen:
                    continue
                seen.add(token)
                ordered.append(token)

        registry_data = self._load_json(self._campaign_registry_path)
        if isinstance(registry_data, dict):
            campaigns = registry_data.get("campaigns") if isinstance(registry_data.get("campaigns"), dict) else {}
            entry = campaigns.get(str(cfg.campaign_id)) if isinstance(campaigns.get(str(cfg.campaign_id)), dict) else {}
            if isinstance(entry, dict):
                _ingest(entry.get("execution_ids") if isinstance(entry.get("execution_ids"), list) else [])

        reports = sorted(self._results_dir.glob("paper_specialized_campaign_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in reports:
            payload = self._load_json(path)
            if not isinstance(payload, dict):
                continue
            scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
            strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
            if str(scope.get("campaign_id") or "") != str(cfg.campaign_id):
                continue
            if str(strategy.get("name") or "") not in {"", cfg.strategy_name}:
                continue
            if str(strategy.get("version") or "") not in {"", cfg.strategy_version}:
                continue
            _ingest(scope.get("execution_ids") if isinstance(scope.get("execution_ids"), list) else [])
            break

        return ordered

    def _load_paper_live_states(self, cfg: PaperCampaignCoverageConfig, execution_ids: list[str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        allowed_execution_ids = set(execution_ids)
        rows_by_context: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for path in sorted(self._results_dir.glob("paper_live_state__*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = self._load_json(path)
            if not isinstance(payload, dict):
                continue
            strategy_name = str(payload.get("strategy_name") or "")
            strategy_version = str(payload.get("strategy_version") or "")
            if strategy_name != cfg.strategy_name or strategy_version != cfg.strategy_version:
                continue

            execution_id = str(payload.get("execution_id") or "").strip()
            if allowed_execution_ids and execution_id and execution_id not in allowed_execution_ids:
                continue

            symbol = str(payload.get("symbol") or "").strip()
            timeframe = str(payload.get("timeframe") or "").strip()
            if not symbol or not timeframe:
                continue
            key = (symbol, timeframe)
            rows_by_context.setdefault(key, []).append(
                {
                    "execution_id": execution_id or None,
                    "cycles": payload.get("cycles"),
                    "last_open_time": payload.get("last_open_time"),
                    "updated_at": payload.get("updated_at"),
                    "path": str(path),
                }
            )
        return rows_by_context

    def _fetch_trade_activity(
        self,
        cfg: PaperCampaignCoverageConfig,
        contexts: list[EdgeDriftContext],
        execution_ids: list[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        strategy_key = f"{cfg.strategy_name}@{cfg.strategy_version}"
        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}

        sql = """
            SELECT id, symbol, timeframe, execution_id, exit_time
            FROM trade_history
            WHERE strategy = :strategy_key
              AND exit_time IS NOT NULL
        """
        params: dict[str, Any] = {"strategy_key": strategy_key}
        stmt = text(sql)
        if execution_ids:
            sql += "\n              AND execution_id IN :execution_ids\n"
            stmt = text(sql).bindparams(bindparam("execution_ids", expanding=True))
            params["execution_ids"] = execution_ids
        sql += "\n            ORDER BY exit_time DESC, id DESC\n"
        stmt = text(sql)
        if execution_ids:
            stmt = stmt.bindparams(bindparam("execution_ids", expanding=True))

        with get_session() as session:
            rows = [dict(row) for row in session.execute(stmt, params).mappings().all()]

        activity: dict[tuple[str, str], dict[str, Any]] = {
            key: {"trades": 0, "last_trade": None, "last_execution_id": None} for key in context_set
        }
        for row in rows:
            key = (str(row.get("symbol") or ""), str(row.get("timeframe") or ""))
            if key not in context_set:
                continue
            item = activity[key]
            item["trades"] = int(item.get("trades") or 0) + 1
            if item.get("last_trade") is None:
                item["last_trade"] = row.get("exit_time")
                item["last_execution_id"] = str(row.get("execution_id") or "").strip() or None
        return activity

    def _fetch_signal_activity(
        self,
        cfg: PaperCampaignCoverageConfig,
        contexts: list[EdgeDriftContext],
        execution_ids: list[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        strategy_key = f"{cfg.strategy_name}@{cfg.strategy_version}"
        context_set = {(ctx.symbol, ctx.timeframe) for ctx in contexts}

        sql = """
            SELECT id, symbol, timeframe, execution_id, timestamp
            FROM signal_snapshots
            WHERE strategy = :strategy_key
        """
        params: dict[str, Any] = {"strategy_key": strategy_key}
        stmt = text(sql)
        if execution_ids:
            sql += "\n              AND execution_id IN :execution_ids\n"
            stmt = text(sql).bindparams(bindparam("execution_ids", expanding=True))
            params["execution_ids"] = execution_ids
        sql += "\n            ORDER BY timestamp DESC, id DESC\n"
        stmt = text(sql)
        if execution_ids:
            stmt = stmt.bindparams(bindparam("execution_ids", expanding=True))

        with get_session() as session:
            rows = [dict(row) for row in session.execute(stmt, params).mappings().all()]

        activity: dict[tuple[str, str], dict[str, Any]] = {
            key: {"signals": 0, "last_signal": None, "last_execution_id": None} for key in context_set
        }
        for row in rows:
            key = (str(row.get("symbol") or ""), str(row.get("timeframe") or ""))
            if key not in context_set:
                continue
            item = activity[key]
            item["signals"] = int(item.get("signals") or 0) + 1
            if item.get("last_signal") is None:
                item["last_signal"] = row.get("timestamp")
                item["last_execution_id"] = str(row.get("execution_id") or "").strip() or None
        return activity

    def _fetch_execution_activity(self, execution_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not execution_ids:
            return {}

        stmt = text(
            """
            SELECT execution_id, started_at, finished_at, status, created_at
            FROM execution_sessions
            WHERE execution_id IN :execution_ids
            """
        ).bindparams(bindparam("execution_ids", expanding=True))
        with get_session() as session:
            rows = [dict(row) for row in session.execute(stmt, {"execution_ids": execution_ids}).mappings().all()]

        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            execution_id = str(row.get("execution_id") or "").strip()
            if not execution_id:
                continue
            output[execution_id] = row
        return output

    def _resolve_active_execution_id(
        self,
        *,
        context_states: list[dict[str, Any]],
        trade_data: dict[str, Any],
        signal_data: dict[str, Any],
    ) -> str | None:
        latest_state = self._pick_latest_state(context_states, None)
        if latest_state and latest_state.get("execution_id"):
            return str(latest_state.get("execution_id"))

        trade_execution = str(trade_data.get("last_execution_id") or "").strip()
        if trade_execution:
            return trade_execution

        signal_execution = str(signal_data.get("last_execution_id") or "").strip()
        if signal_execution:
            return signal_execution
        return None

    def _pick_latest_state(self, context_states: list[dict[str, Any]], execution_id: str | None) -> dict[str, Any] | None:
        selected = context_states
        if execution_id:
            selected = [row for row in context_states if str(row.get("execution_id") or "") == execution_id]
            if not selected:
                selected = context_states

        if not selected:
            return None

        def _sort_key(row: dict[str, Any]) -> tuple[datetime, datetime]:
            updated = _parse_dt(row.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
            last_open = _parse_dt(row.get("last_open_time")) or datetime.min.replace(tzinfo=timezone.utc)
            return (updated, last_open)

        return sorted(selected, key=_sort_key, reverse=True)[0]

    def _write_outputs(self, output_prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        csv_path = self._results_dir / f"{output_prefix}_{stamp}_contexts.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        self._write_contexts_csv(csv_path, report.get("contexts", []))

        return {
            "json": str(json_path),
            "md": str(md_path),
            "csv": str(csv_path),
        }

    def _to_markdown(self, report: dict[str, Any]) -> str:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        strategy = report.get("strategy") if isinstance(report.get("strategy"), dict) else {}
        scope = report.get("scope") if isinstance(report.get("scope"), dict) else {}

        lines = [
            "# Paper Campaign Coverage",
            "",
            f"- Campaign ID: {scope.get('campaign_id')}",
            f"- Strategy: {strategy.get('name')}",
            f"- Version: {strategy.get('version')}",
            f"- Contexts approved: {summary.get('approved_contexts')}",
            f"- Contexts active: {summary.get('active_contexts')}",
            f"- Coverage: {summary.get('coverage_percent')}%",
            f"- Coverage sufficient: {summary.get('coverage_sufficient')}",
            f"- Largest gap: {summary.get('largest_gap', {}).get('symbol')} {summary.get('largest_gap', {}).get('timeframe')}",
            f"- Largest gap time: {summary.get('largest_gap', {}).get('formatted')}",
            "",
            "## Alerts",
        ]

        alerts = report.get("alerts") if isinstance(report.get("alerts"), dict) else {}
        context_alerts = alerts.get("context_alerts") if isinstance(alerts.get("context_alerts"), list) else []
        global_alerts = alerts.get("global_alerts") if isinstance(alerts.get("global_alerts"), list) else []

        if not context_alerts and not global_alerts:
            lines.append("- No alerts")
        else:
            for alert in global_alerts:
                lines.append(f"- {alert.get('type')}: coverage={alert.get('coverage_percent')}")
            for alert in context_alerts:
                lines.append(
                    f"- {alert.get('type')}: {alert.get('symbol')} {alert.get('timeframe')} minutes={alert.get('minutes_without_activity')}"
                )

        lines.extend(["", "## Contexts"])
        for row in report.get("contexts", []):
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- {row.get('symbol')} {row.get('timeframe')}: status={row.get('status')} "
                f"minutes_without_activity={row.get('minutes_without_activity')} trades={row.get('trades')}"
            )
        return "\n".join(lines) + "\n"

    def _write_contexts_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        fieldnames = [
            "symbol",
            "timeframe",
            "execution_id",
            "status",
            "last_activity",
            "minutes_without_activity",
            "trades",
            "last_trade",
            "last_signal",
            "last_cycle_executed",
            "last_candle_processed",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow({field: row.get(field) for field in fieldnames})

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, (dict, list)) else None

    @staticmethod
    def _coerce_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
