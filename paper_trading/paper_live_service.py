"""Continuous paper trading operation with version management and operational reports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
import platform
import socket
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import settings
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.models import Trade
from execution.hypothesis_runtime import hypothesis_gate_config_from_payload, wrap_strategy_with_hypothesis
from paper_trading.campaign_registry_store import upsert_campaign_registry_execution
from paper_trading.daily_report import PaperDailyReportConfig, PaperDailyReportService
from paper_trading.paper_broker import PaperBroker
from paper_trading.paper_trader import PaperTrader
from strategies.factory import create_strategy
from utils.atomic_io import atomic_write_text
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PaperLiveConfig:
    symbol: str
    timeframe: str
    strategy_name: str
    strategy_version: str = "v1.0"
    campaign_id: str | None = None
    initial_capital: float = 10_000.0
    poll_seconds: float = 15.0
    bootstrap_bars: int = 1500
    bootstrap_replay_bars: int = 350
    max_cycles: int = 0
    resume: bool = True
    min_trades_before_change: int = 100
    report_min_interval_seconds: int = 900
    report_on_trade_change: bool = True
    hypothesis_config: dict[str, Any] | None = None
    output_prefix: str = "paper_live"
    max_frame_bars: int = 3000


class StrategyVersionManager:
    """Persistent strategy version registry and comparison service."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register_and_activate(self, strategy_name: str, version: str, description: str | None = None) -> None:
        history = HistoryPersistenceService(self._session)
        history.register_strategy_version(
            strategy_name=strategy_name,
            version=version,
            git_commit=os.getenv("GIT_COMMIT"),
            description=description,
        )
        self._session.execute(
            text(
                """
                UPDATE strategy_versions
                SET active = CASE WHEN version = :version THEN 1 ELSE 0 END
                WHERE strategy_name = :strategy_name
                """
            ),
            {"strategy_name": strategy_name, "version": version},
        )
        self._session.commit()

    def ensure_change_allowed(
        self,
        *,
        strategy_name: str,
        target_version: str,
        min_trades_before_change: int,
    ) -> dict[str, Any]:
        if int(min_trades_before_change) <= 0:
            return {
                "allowed": True,
                "reason": "gate_disabled",
                "previous_version": None,
                "previous_trades": 0,
            }

        previous = self._session.execute(
            text(
                """
                SELECT version
                FROM strategy_versions
                WHERE strategy_name = :strategy_name
                  AND version <> :target_version
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"strategy_name": strategy_name, "target_version": target_version},
        ).fetchone()

        if previous is None:
            return {
                "allowed": True,
                "reason": "first_version",
                "previous_version": None,
                "previous_trades": 0,
            }

        previous_version = str(previous[0])
        previous_key = f"{strategy_name}@{previous_version}"
        previous_trades = int(
            self._session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM trade_history
                    WHERE strategy = :strategy_key
                    """
                ),
                {"strategy_key": previous_key},
            ).scalar()
            or 0
        )

        if previous_trades < int(min_trades_before_change):
            return {
                "allowed": False,
                "reason": "insufficient_previous_trades",
                "previous_version": previous_version,
                "previous_trades": previous_trades,
                "min_required": int(min_trades_before_change),
            }

        return {
            "allowed": True,
            "reason": "min_trades_satisfied",
            "previous_version": previous_version,
            "previous_trades": previous_trades,
        }

    def compare_versions(
        self,
        *,
        strategy_name: str,
        current_version: str,
        base_version: str | None = None,
        window_days: int = 30,
    ) -> dict[str, Any]:
        if not base_version:
            row = self._session.execute(
                text(
                    """
                    SELECT version
                    FROM strategy_versions
                    WHERE strategy_name = :strategy_name
                      AND version <> :current_version
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"strategy_name": strategy_name, "current_version": current_version},
            ).fetchone()
            base_version = str(row[0]) if row else None

        if base_version is None:
            return {
                "status": "inconclusive",
                "reason": "no_base_version",
                "current_version": current_version,
            }

        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=max(1, int(window_days)))

        current_metrics = self._metrics_for_version(strategy_name, current_version, start_dt, end_dt)
        base_metrics = self._metrics_for_version(strategy_name, base_version, start_dt, end_dt)

        status = "inconclusive"
        if current_metrics["trades"] >= 10 and base_metrics["trades"] >= 10:
            improved_pf = current_metrics["profit_factor"] >= (base_metrics["profit_factor"] * 1.05)
            improved_net = current_metrics["net_profit"] >= base_metrics["net_profit"]
            safer_dd = current_metrics["drawdown"] <= (base_metrics["drawdown"] * 1.05)
            worse_pf = current_metrics["profit_factor"] <= (base_metrics["profit_factor"] * 0.95)
            worse_net = current_metrics["net_profit"] < base_metrics["net_profit"]

            if improved_pf and improved_net and safer_dd:
                status = "improved"
            elif worse_pf and worse_net:
                status = "worsened"

        return {
            "status": status,
            "strategy_name": strategy_name,
            "current_version": current_version,
            "base_version": base_version,
            "window_days": int(window_days),
            "current_metrics": current_metrics,
            "base_metrics": base_metrics,
        }

    def _metrics_for_version(
        self,
        strategy_name: str,
        version: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        strategy_key = f"{strategy_name}@{version}"
        rows = self._session.execute(
            text(
                """
                SELECT pnl, pnl_percent
                FROM trade_history
                WHERE strategy = :strategy_key
                  AND exit_time >= :start_dt
                  AND exit_time < :end_dt
                """
            ),
            {
                "strategy_key": strategy_key,
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
        ).fetchall()

        pnl_values = [float(row[0] or 0.0) for row in rows]
        pnl_pct_values = [float(row[1] or 0.0) for row in rows]
        wins = [v for v in pnl_values if v > 0]
        losses = [v for v in pnl_values if v <= 0]
        gross_profit = sum(wins)
        gross_loss_abs = abs(sum(losses))
        pf = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)
        trades = len(pnl_values)
        win_rate = (len(wins) / trades) if trades else 0.0
        net = sum(pnl_values)
        expectancy = (net / trades) if trades else 0.0
        drawdown_proxy = abs(min([0.0] + pnl_pct_values))
        stability = 1.0 - min(1.0, drawdown_proxy)
        robustness = max(0.0, min(2.0, pf / 2.0))

        return {
            "trades": trades,
            "win_rate": round(win_rate, 6),
            "profit_factor": round(pf, 6),
            "net_profit": round(net, 6),
            "expectancy": round(expectancy, 6),
            "drawdown": round(drawdown_proxy, 6),
            "stability": round(stability, 6),
            "robustness": round(robustness, 6),
        }


class PaperOperationalReportingService:
    """Generates operation, hourly, daily, weekly and monthly reports."""

    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def generate_all(
        self,
        *,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        outputs: dict[str, str] = {}
        outputs.update(self.generate_operation_report(report_date, strategy_name, strategy_version, output_prefix))
        outputs.update(self.generate_hourly_report(report_date, strategy_name, strategy_version, output_prefix))

        daily_service = PaperDailyReportService(self._session, self._base_dir)
        daily_result = daily_service.run(
            PaperDailyReportConfig(
                report_date=report_date,
                strategy_name=f"{strategy_name}@{strategy_version}",
                output_prefix=f"{output_prefix}_daily",
            )
        )
        outputs.update({f"daily_{k}": v for k, v in daily_result.get("outputs", {}).items()})

        outputs.update(self.generate_weekly_report(report_date, strategy_name, strategy_version, output_prefix))
        outputs.update(self.generate_monthly_report(report_date, strategy_name, strategy_version, output_prefix))
        return outputs

    def generate_operation_report(
        self,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        start_dt = datetime.combine(report_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1)
        strategy_key = f"{strategy_name}@{strategy_version}"

        rows = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT th.execution_id, th.symbol, th.timeframe, th.entry_time, th.exit_time,
                       th.entry_price, th.exit_price, th.stop_loss, th.take_profit,
                       th.duration_minutes, th.pnl, th.pnl_percent, th.score,
                       th.exit_reason, ss.entry_price AS signal_entry_price,
                       ss.score AS signal_score, ss.rr AS signal_rr, ss.market_regime,
                       ss.rejection_reason
                FROM trade_history th
                LEFT JOIN signal_snapshots ss
                  ON ss.strategy = th.strategy
                 AND ss.symbol = th.symbol
                 AND ss.timeframe = th.timeframe
                 AND ss.timestamp = th.entry_time
                 AND ss.`signal` = 'BUY'
                WHERE th.strategy = :strategy_key
                  AND th.exit_time >= :start_dt
                  AND th.exit_time < :end_dt
                ORDER BY th.exit_time ASC
                """
            ),
            {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt},
        ).mappings().all()]

        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{output_prefix}_operation_report_latest.json"
        md_path = out_dir / f"{output_prefix}_operation_report_latest.md"

        payload = {
            "report_date": report_date.isoformat(),
            "strategy_name": strategy_name,
            "strategy_version": strategy_version,
            "operations": rows,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        md_lines = [
            "# Operation Report",
            "",
            f"Date: {report_date.isoformat()}",
            f"Strategy: {strategy_name}@{strategy_version}",
            "",
            f"Operations: {len(rows)}",
            "",
        ]
        for idx, row in enumerate(rows, start=1):
            md_lines.append(f"## Operation {idx}")
            md_lines.append(f"- Symbol: {row.get('symbol')}")
            md_lines.append(f"- Timeframe: {row.get('timeframe')}")
            md_lines.append(f"- Entry time: {row.get('entry_time')}")
            md_lines.append(f"- Exit time: {row.get('exit_time')}")
            md_lines.append(f"- Entry price: {row.get('entry_price')}")
            md_lines.append(f"- Exit price: {row.get('exit_price')}")
            md_lines.append(f"- Stop: {row.get('stop_loss')}")
            md_lines.append(f"- Take profit: {row.get('take_profit')}")
            md_lines.append(f"- Duration minutes: {row.get('duration_minutes')}")
            md_lines.append(f"- PnL: {row.get('pnl')}")
            md_lines.append(f"- Score: {row.get('score')}")
            md_lines.append(f"- Entry reason: strategy_signal_score={row.get('signal_score')}")
            md_lines.append(f"- Exit reason: {row.get('exit_reason')}")
            md_lines.append(f"- Indicator context: rr={row.get('signal_rr')} regime={row.get('market_regime')}")
            md_lines.append("")
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        return {
            "operation_report_json": str(json_path),
            "operation_report_md": str(md_path),
        }

    def generate_hourly_report(
        self,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        start_dt = datetime.combine(report_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1)
        strategy_key = f"{strategy_name}@{strategy_version}"

        rows = [dict(row) for row in self._session.execute(
            text(
                """
                SELECT DATE_FORMAT(entry_time, '%Y-%m-%d %H:00:00') AS hour_bucket,
                       COUNT(*) AS operations,
                       SUM(pnl) AS net_profit,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
                       ABS(SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END)) AS gross_loss
                FROM trade_history
                WHERE strategy = :strategy_key
                  AND exit_time >= :start_dt
                  AND exit_time < :end_dt
                GROUP BY DATE_FORMAT(entry_time, '%Y-%m-%d %H:00:00')
                ORDER BY hour_bucket ASC
                """
            ),
            {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt},
        ).mappings().all()]

        for row in rows:
            ops = int(row.get("operations") or 0)
            wins = int(row.get("wins") or 0)
            gp = float(row.get("gross_profit") or 0.0)
            gl = float(row.get("gross_loss") or 0.0)
            row["win_rate"] = (wins / ops) if ops else 0.0
            row["profit_factor"] = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)

        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{output_prefix}_hourly_report_latest.json"
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        return {"hourly_report_json": str(json_path)}

    def generate_weekly_report(
        self,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        end_current = datetime.combine(report_date, datetime.max.time()).replace(tzinfo=timezone.utc)
        start_current = end_current - timedelta(days=6)
        end_prev = start_current - timedelta(seconds=1)
        start_prev = end_prev - timedelta(days=6)

        payload = {
            "current_7d": self._period_metrics(strategy_name, strategy_version, start_current, end_current),
            "previous_7d": self._period_metrics(strategy_name, strategy_version, start_prev, end_prev),
        }

        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{output_prefix}_weekly_report_latest.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"weekly_report_json": str(json_path)}

    def generate_monthly_report(
        self,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        end_current = datetime.combine(report_date, datetime.max.time()).replace(tzinfo=timezone.utc)
        start_current = end_current - timedelta(days=29)
        end_prev = start_current - timedelta(seconds=1)
        start_prev = end_prev - timedelta(days=29)

        payload = {
            "current_30d": self._period_metrics(strategy_name, strategy_version, start_current, end_current),
            "previous_30d": self._period_metrics(strategy_name, strategy_version, start_prev, end_prev),
        }

        out_dir = self._base_dir / "optimization" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{output_prefix}_monthly_report_latest.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"monthly_report_json": str(json_path)}

    def _period_metrics(
        self,
        strategy_name: str,
        strategy_version: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, Any]:
        strategy_key = f"{strategy_name}@{strategy_version}"
        rows = self._session.execute(
            text(
                """
                SELECT pnl, pnl_percent
                FROM trade_history
                WHERE strategy = :strategy_key
                  AND exit_time >= :start_dt
                  AND exit_time <= :end_dt
                """
            ),
            {"strategy_key": strategy_key, "start_dt": start_dt, "end_dt": end_dt},
        ).fetchall()

        pnl_values = [float(row[0] or 0.0) for row in rows]
        pnl_pct = [float(row[1] or 0.0) for row in rows]
        wins = [p for p in pnl_values if p > 0]
        losses = [p for p in pnl_values if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        net = sum(pnl_values)
        trades = len(pnl_values)
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        win_rate = (len(wins) / trades) if trades else 0.0
        expectancy = (net / trades) if trades else 0.0
        drawdown = abs(min([0.0] + pnl_pct))

        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "trades": trades,
            "net_profit": round(net, 6),
            "win_rate": round(win_rate, 6),
            "profit_factor": round(pf, 6),
            "expectancy": round(expectancy, 6),
            "drawdown": round(drawdown, 6),
        }


class PaperLiveService:
    """Runs continuous paper trading by monitoring new candles in DB."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._state_dir = self._base_dir / "optimization" / "results"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._campaign_registry_path = self._state_dir / "paper_specialized_campaign_registry.json"

    def run(self, cfg: PaperLiveConfig) -> dict[str, Any]:
        started = datetime.now(tz=timezone.utc)
        state_key = self._state_key(cfg.strategy_name, cfg.symbol, cfg.timeframe)
        state = self._load_state(state_key) if cfg.resume else {}
        paper_start_timestamp = self._state_datetime(state.get("paper_start_timestamp")) or started
        effective_hypothesis = self._effective_hypothesis_payload(
            config_payload=cfg.hypothesis_config,
            state_payload=state.get("hypothesis_config") if isinstance(state, dict) else None,
        )

        persisted_campaign_id = str(state.get("campaign_id") or "").strip()
        requested_campaign_id = str(cfg.campaign_id or "").strip()
        if persisted_campaign_id and requested_campaign_id and persisted_campaign_id != requested_campaign_id:
            raise RuntimeError(
                "Campaign ID mismatch for resumed PaperLive state: "
                f"state_campaign_id={persisted_campaign_id} requested_campaign_id={requested_campaign_id}"
            )
        effective_campaign_id = requested_campaign_id or persisted_campaign_id or None

        execution_id = str(state.get("execution_id") or HistoryPersistenceService.new_execution_id())
        if effective_campaign_id:
            self._link_execution_to_campaign(
                campaign_id=str(effective_campaign_id),
                strategy_name=cfg.strategy_name,
                strategy_version=cfg.strategy_version,
                execution_id=execution_id,
            )
        strategy = self._build_runtime_strategy(cfg, hypothesis_payload=effective_hypothesis)

        broker = PaperBroker(initial_capital=cfg.initial_capital)
        trader = PaperTrader(
            strategy=strategy,
            broker=broker,
            execution_id=execution_id,
            timeframe=cfg.timeframe,
            strategy_version=cfg.strategy_version,
            campaign_id=effective_campaign_id,
        )

        with get_session() as session:
            manager = StrategyVersionManager(session)
            gate = manager.ensure_change_allowed(
                strategy_name=cfg.strategy_name,
                target_version=cfg.strategy_version,
                min_trades_before_change=cfg.min_trades_before_change,
            )
            if not gate.get("allowed", False):
                raise RuntimeError(
                    "Strategy change blocked by min trades gate: "
                    f"previous_version={gate.get('previous_version')} "
                    f"previous_trades={gate.get('previous_trades')} "
                    f"min_required={gate.get('min_required')}"
                )
            manager.register_and_activate(
                strategy_name=cfg.strategy_name,
                version=cfg.strategy_version,
                description="Paper live continuous operation",
            )

            history = HistoryPersistenceService(session)
            history.start_execution_session(
                execution_id=execution_id,
                started_at=started,
                status="running",
                host=socket.gethostname(),
                cpu=platform.processor(),
                workers=1,
                python_version=platform.python_version(),
                git_version=os.getenv("GIT_COMMIT"),
            )

        restored_open_trade = trader.restore_open_trade(cfg.symbol)
        if restored_open_trade:
            logger.info("paper-live resume restored open trade from DB")
        trader.import_runtime_state(state.get("runtime_state") if isinstance(state, dict) else None)

        frame = self._load_recent_candles(cfg.symbol, cfg.timeframe, cfg.bootstrap_bars)
        if frame.empty:
            raise RuntimeError(
                f"No candles found for {cfg.symbol}/{cfg.timeframe}. Run download before paper-live."
            )
        frame = self._bound_frame(frame, cfg.max_frame_bars)
        if state and not trader.export_runtime_state().get("last_strategy_evaluation"):
            trader.refresh_telemetry(frame)

        last_open_time = self._state_datetime(state.get("last_open_time"))
        is_new_execution = not bool(state)
        if is_new_execution:
            # Historical candles warm indicators only; Paper starts at the next closed candle.
            last_open_time = frame.index[-1].to_pydatetime()
            process_start_index = len(frame)
        else:
            process_start_index = self._resolve_start_index(frame, last_open_time, cfg.bootstrap_replay_bars)
        cycles = int(state.get("cycles") or 0)
        last_report_at = self._state_datetime(state.get("last_report_at"))
        last_report_closed_trades = int(state.get("last_report_closed_trades") or 0)
        report_outputs = state.get("report_outputs") if isinstance(state.get("report_outputs"), dict) else {}
        stats = {
            "execution_id": execution_id,
            "strategy": cfg.strategy_name,
            "strategy_version": cfg.strategy_version,
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "processed_bars": 0,
            "closed_trades": 0,
            "last_open_time": last_open_time.isoformat() if last_open_time else None,
        }

        try:
            while True:
                cycles += 1
                processed_in_cycle = self._process_new_bars(
                    trader=trader,
                    frame=frame,
                    symbol=cfg.symbol,
                    start_index=process_start_index,
                    timeframe=cfg.timeframe,
                    paper_start_timestamp=paper_start_timestamp,
                )
                stats["processed_bars"] = int(stats["processed_bars"]) + processed_in_cycle

                if len(frame.index) > 0:
                    last_open_time = frame.index[-1].to_pydatetime() if hasattr(frame.index[-1], "to_pydatetime") else frame.index[-1]
                    stats["last_open_time"] = str(last_open_time)

                current_closed_trades = int(trader._stats.get("closed_trades", 0))
                if self._should_generate_reports(
                    now=datetime.now(tz=timezone.utc),
                    last_report_at=last_report_at,
                    current_closed_trades=current_closed_trades,
                    last_report_closed_trades=last_report_closed_trades,
                    min_interval_seconds=max(1, int(cfg.report_min_interval_seconds)),
                    on_trade_change=bool(cfg.report_on_trade_change),
                ):
                    report_outputs = self._generate_operational_reports(
                        report_date=datetime.now(tz=timezone.utc).date(),
                        strategy_name=cfg.strategy_name,
                        strategy_version=cfg.strategy_version,
                        output_prefix=cfg.output_prefix,
                    )
                    last_report_at = datetime.now(tz=timezone.utc)
                    last_report_closed_trades = current_closed_trades

                self._save_state(
                    state_key,
                    {
                        "execution_id": execution_id,
                        "paper_start_timestamp": paper_start_timestamp.isoformat(),
                        "symbol": cfg.symbol,
                        "timeframe": cfg.timeframe,
                        "strategy_name": cfg.strategy_name,
                        "strategy_version": cfg.strategy_version,
                        "campaign_id": effective_campaign_id,
                        "last_open_time": stats["last_open_time"],
                        "cycles": cycles,
                        "report_outputs": report_outputs,
                        "last_report_at": last_report_at.isoformat() if last_report_at else None,
                        "last_report_closed_trades": int(last_report_closed_trades),
                        "runtime_state": trader.export_runtime_state(),
                        "hypothesis_config": effective_hypothesis,
                        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )

                latest = self._load_candles_after(cfg.symbol, cfg.timeframe, last_open_time)
                if not latest.empty:
                    frame = pd.concat([frame, latest]).sort_index()
                    frame = frame[~frame.index.duplicated(keep="last")]
                    frame = self._bound_frame(frame, cfg.max_frame_bars)
                    process_start_index = max(0, len(frame) - len(latest))
                else:
                    process_start_index = len(frame)
                    if cfg.max_cycles > 0 and cycles >= cfg.max_cycles:
                        break
                    sleep(max(1.0, float(cfg.poll_seconds)))

                if cfg.max_cycles > 0 and cycles >= cfg.max_cycles:
                    break

            stats["status"] = "completed"
        except KeyboardInterrupt:
            logger.warning("paper-live interrupted by user")
            stats["status"] = "interrupted"
        except Exception:
            stats["status"] = "failed"
            raise
        finally:
            stats["opened_orders"] = int(trader._stats.get("entries", 0))
            stats["closed_trades"] = int(trader._stats.get("closed_trades", stats["closed_trades"]))
            stats["rejected_entries"] = int(trader._stats.get("rejected_entries", 0))
            stats["signals"] = int(trader._stats.get("entries", 0)) + int(trader._stats.get("rejected_entries", 0))
            finished = datetime.now(tz=timezone.utc)
            duration = perf_counter()
            with get_session() as session:
                history = HistoryPersistenceService(session)
                history.finish_execution_session(
                    execution_id=execution_id,
                    finished_at=finished,
                    duration=duration,
                    status=str(stats.get("status", "completed")),
                )

        with get_session() as session:
            manager = StrategyVersionManager(session)
            stats["version_comparison"] = manager.compare_versions(
                strategy_name=cfg.strategy_name,
                current_version=cfg.strategy_version,
            )

        return stats

    def _build_runtime_strategy(self, cfg: PaperLiveConfig, *, hypothesis_payload: dict[str, Any] | None = None):
        payload = (
            hypothesis_payload
            if isinstance(hypothesis_payload, dict)
            else (cfg.hypothesis_config if isinstance(cfg.hypothesis_config, dict) else {})
        )
        params_raw = payload.get("approved_parameters") if isinstance(payload, dict) else {}
        strategy_params = dict(params_raw) if isinstance(params_raw, dict) else {}

        strategy = create_strategy(cfg.strategy_name, **strategy_params)
        strategy = wrap_strategy_with_hypothesis(
            strategy,
            hypothesis_gate_config_from_payload(payload),
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
        )
        strategy.initialize()
        return strategy

    @staticmethod
    def _effective_hypothesis_payload(
        *,
        config_payload: dict[str, Any] | None,
        state_payload: Any,
    ) -> dict[str, Any]:
        if isinstance(config_payload, dict):
            return dict(config_payload)
        if isinstance(state_payload, dict):
            return dict(state_payload)
        return {}

    @staticmethod
    def _bound_frame(frame: pd.DataFrame, max_bars: int) -> pd.DataFrame:
        cap = max(200, int(max_bars or 0))
        if len(frame) <= cap:
            return frame
        return frame.tail(cap).copy()

    def _link_execution_to_campaign(
        self,
        *,
        campaign_id: str,
        strategy_name: str,
        strategy_version: str,
        execution_id: str,
    ) -> None:
        if not campaign_id or not execution_id:
            return
        upsert_campaign_registry_execution(
            registry_path=self._campaign_registry_path,
            campaign_id=campaign_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            execution_ids=[execution_id],
        )

    @staticmethod
    def _should_generate_reports(
        *,
        now: datetime,
        last_report_at: datetime | None,
        current_closed_trades: int,
        last_report_closed_trades: int,
        min_interval_seconds: int,
        on_trade_change: bool,
    ) -> bool:
        if last_report_at is None:
            return True

        elapsed_seconds = max(0.0, (now - last_report_at).total_seconds())
        if elapsed_seconds >= float(max(1, int(min_interval_seconds))):
            return True

        if on_trade_change and int(current_closed_trades) > int(last_report_closed_trades):
            return True

        return False

    def compare_versions(
        self,
        *,
        strategy_name: str,
        current_version: str,
        base_version: str | None,
        window_days: int,
    ) -> dict[str, Any]:
        with get_session() as session:
            manager = StrategyVersionManager(session)
            return manager.compare_versions(
                strategy_name=strategy_name,
                current_version=current_version,
                base_version=base_version,
                window_days=window_days,
            )

    def generate_reports(
        self,
        *,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        return self._generate_operational_reports(
            report_date=report_date,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            output_prefix=output_prefix,
        )

    def _generate_operational_reports(
        self,
        *,
        report_date: date,
        strategy_name: str,
        strategy_version: str,
        output_prefix: str,
    ) -> dict[str, str]:
        with get_session() as session:
            reporter = PaperOperationalReportingService(session, self._base_dir)
            return reporter.generate_all(
                report_date=report_date,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                output_prefix=output_prefix,
            )

    def _load_recent_candles(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        rows = []
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT open_time, open, high, low, close, volume
                    FROM candles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                    ORDER BY open_time DESC
                    LIMIT :lim
                    """
                ),
                {"symbol": symbol, "timeframe": timeframe, "lim": int(max(100, limit))},
            ).fetchall()

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        records = list(reversed(rows))
        frame = pd.DataFrame(
            [{"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])} for r in records],
            index=pd.DatetimeIndex([r[0] for r in records], tz="UTC"),
        )
        return self._closed_candles(frame, timeframe)

    def _load_candles_after(self, symbol: str, timeframe: str, last_open_time: Any) -> pd.DataFrame:
        if last_open_time is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        rows = []
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT open_time, open, high, low, close, volume
                    FROM candles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                      AND open_time > :last_open_time
                    ORDER BY open_time ASC
                    LIMIT 1500
                    """
                ),
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "last_open_time": last_open_time,
                },
            ).fetchall()

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        frame = pd.DataFrame(
            [{"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])} for r in rows],
            index=pd.DatetimeIndex([r[0] for r in rows], tz="UTC"),
        )
        return self._closed_candles(frame, timeframe)

    @staticmethod
    def _closed_candles(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        duration = pd.Timedelta(timeframe)
        now = pd.Timestamp.now(tz="UTC")
        return frame.loc[frame.index + duration <= now]

    def _resolve_start_index(self, frame: pd.DataFrame, last_open_time: datetime | None, replay_bars: int) -> int:
        if frame.empty:
            return 0
        if last_open_time is not None:
            for idx, ts in enumerate(frame.index):
                ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                if ts_dt > last_open_time:
                    return max(0, idx)
            return len(frame)

        # First run: replay only a recent window to keep startup practical.
        return max(50, len(frame) - max(60, int(replay_bars)))

    @staticmethod
    def _process_new_bars(
        *,
        trader: PaperTrader,
        frame: pd.DataFrame,
        symbol: str,
        start_index: int,
        timeframe: str,
        paper_start_timestamp: datetime,
    ) -> int:
        if frame.empty:
            return 0
        processed = 0
        for i in range(max(50, int(start_index)), len(frame)):
            candle_close = frame.index[i] + pd.Timedelta(timeframe)
            if candle_close <= pd.Timestamp(paper_start_timestamp):
                continue
            window = frame.iloc[: i + 1]
            trader.on_bar(window, symbol)
            processed += 1
        return processed

    @staticmethod
    def _state_key(strategy_name: str, symbol: str, timeframe: str) -> str:
        safe_strategy = str(strategy_name).strip().replace("/", "_").replace(" ", "_")
        safe_symbol = str(symbol).strip().replace("/", "_").replace(" ", "_")
        safe_timeframe = str(timeframe).strip().replace("/", "_").replace(" ", "_")
        return f"{safe_strategy}__{safe_symbol}__{safe_timeframe}"

    def _state_file_for(self, state_key: str) -> Path:
        return self._state_dir / f"paper_live_state__{state_key}.json"

    def _load_state(self, state_key: str) -> dict[str, Any]:
        state_file = self._state_file_for(state_key)
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = state_file.with_suffix(state_file.suffix + ".bak")
            if not backup.exists():
                return {}
            try:
                payload = json.loads(backup.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}

    def _save_state(self, state_key: str, state: dict[str, Any]) -> None:
        state_file = self._state_file_for(state_key)
        payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
        atomic_write_text(state_file, payload, encoding="utf-8")
        atomic_write_text(state_file.with_suffix(state_file.suffix + ".bak"), payload, encoding="utf-8")

    @staticmethod
    def _state_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None
