"""Supervisor for resilient PaperLive campaign execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from multiprocessing import Process
from pathlib import Path
from time import sleep
from typing import Any, Callable, Protocol

from paper_trading.edge_drift_monitor import EdgeDriftContext
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService


@dataclass(frozen=True)
class PaperLiveSupervisorConfig:
    strategy_name: str = "ClassicDonchianBreakout"
    strategy_version: str = "v1.0"
    campaign_id: str = ""
    contexts: tuple[EdgeDriftContext, ...] = ()
    contexts_from_latest_report: bool = True
    initial_capital: float = 10_000.0
    poll_seconds: float = 15.0
    bootstrap_bars: int = 1500
    bootstrap_replay_bars: int = 350
    min_trades_before_change: int = 0
    output_prefix: str = "paper_live"
    supervisor_poll_seconds: float = 10.0
    stuck_timeout_seconds: float = 600.0
    startup_grace_seconds: float = 120.0
    restart_delay_seconds: float = 2.0
    max_consecutive_restarts: int = 5
    max_supervision_cycles: int = 0
    hypothesis_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class PaperLiveLaunchConfig:
    symbol: str
    timeframe: str
    strategy_name: str
    strategy_version: str
    campaign_id: str
    initial_capital: float
    poll_seconds: float
    bootstrap_bars: int
    bootstrap_replay_bars: int
    min_trades_before_change: int
    output_prefix: str
    hypothesis_config: dict[str, Any] | None = None
    resume: bool = True


class WorkerHandle(Protocol):
    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


class ProcessWorkerHandle:
    def __init__(self, process: Process) -> None:
        self._process = process

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def terminate(self) -> None:
        if self._process.is_alive():
            self._process.terminate()

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout=timeout)


@dataclass
class _ContextRuntime:
    context: EdgeDriftContext
    launch_cfg: PaperLiveLaunchConfig
    worker: WorkerHandle | None = None
    started_at: datetime | None = None
    last_progress_token: tuple[Any, ...] | None = None
    last_progress_at: datetime | None = None
    consecutive_restarts: int = 0
    total_restarts: int = 0
    permanent_failure: bool = False


@dataclass(frozen=True)
class SupervisorProgressSnapshot:
    cycles: int | None
    last_open_time: str | None
    updated_at: str | None

    @property
    def token(self) -> tuple[Any, ...]:
        return (self.cycles, self.last_open_time, self.updated_at)


class PaperLiveSupervisorService:
    def __init__(
        self,
        base_dir: Path,
        *,
        launcher: Callable[[PaperLiveLaunchConfig, Path], WorkerHandle] | None = None,
        progress_reader: Callable[[Path, PaperLiveLaunchConfig], SupervisorProgressSnapshot | None] | None = None,
        time_provider: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._launcher = launcher or self._default_launcher
        self._progress_reader = progress_reader or self._default_progress_reader
        self._time_provider = time_provider or (lambda: datetime.now(tz=timezone.utc))
        self._sleeper = sleeper or sleep

    def run(self, cfg: PaperLiveSupervisorConfig) -> dict[str, Any]:
        contexts = self._resolve_contexts(cfg)
        if not contexts:
            raise RuntimeError("No campaign contexts available for paper-live supervisor.")

        runtimes = {
            self._context_key(ctx): _ContextRuntime(
                context=ctx,
                launch_cfg=PaperLiveLaunchConfig(
                    symbol=ctx.symbol,
                    timeframe=ctx.timeframe,
                    strategy_name=cfg.strategy_name,
                    strategy_version=cfg.strategy_version,
                    campaign_id=str(cfg.campaign_id),
                    initial_capital=max(100.0, float(cfg.initial_capital)),
                    poll_seconds=max(1.0, float(cfg.poll_seconds)),
                    bootstrap_bars=max(200, int(cfg.bootstrap_bars)),
                    bootstrap_replay_bars=max(60, int(cfg.bootstrap_replay_bars)),
                    min_trades_before_change=max(0, int(cfg.min_trades_before_change)),
                    output_prefix=str(cfg.output_prefix),
                    hypothesis_config=dict(cfg.hypothesis_config) if isinstance(cfg.hypothesis_config, dict) else None,
                    resume=True,
                ),
            )
            for ctx in contexts
        }

        started_at = self._time_provider()
        stamp = started_at.strftime("%Y%m%d_%H%M%S")
        audit_jsonl = self._results_dir / f"paper_live_supervisor_audit_{stamp}.jsonl"
        summary_events: list[dict[str, Any]] = []

        for runtime in runtimes.values():
            self._start_context(runtime, audit_jsonl, summary_events, reason="INITIAL_START")

        cycles = 0
        status = "completed"
        try:
            while True:
                cycles += 1
                now = self._time_provider()

                for runtime in runtimes.values():
                    if runtime.permanent_failure:
                        continue

                    if runtime.worker is None or not runtime.worker.is_alive():
                        self._restart_context(
                            runtime,
                            cfg=cfg,
                            now=now,
                            audit_jsonl=audit_jsonl,
                            summary_events=summary_events,
                            reason="PROCESS_EXITED",
                        )
                        continue

                    snapshot = self._progress_reader(self._results_dir, runtime.launch_cfg)
                    if snapshot is not None:
                        token = snapshot.token
                        if runtime.last_progress_token != token:
                            runtime.last_progress_token = token
                            runtime.last_progress_at = now
                            runtime.consecutive_restarts = 0

                    if runtime.started_at is not None and now - runtime.started_at < timedelta(seconds=max(1.0, float(cfg.startup_grace_seconds))):
                        continue

                    if runtime.last_progress_at is None:
                        continue

                    stall_seconds = (now - runtime.last_progress_at).total_seconds()
                    if stall_seconds >= float(max(1.0, cfg.stuck_timeout_seconds)):
                        self._restart_context(
                            runtime,
                            cfg=cfg,
                            now=now,
                            audit_jsonl=audit_jsonl,
                            summary_events=summary_events,
                            reason="STUCK_NO_PROGRESS",
                        )

                if cfg.max_supervision_cycles > 0 and cycles >= int(cfg.max_supervision_cycles):
                    break

                self._sleeper(max(0.1, float(cfg.supervisor_poll_seconds)))
        except KeyboardInterrupt:
            status = "interrupted"
        finally:
            for runtime in runtimes.values():
                if runtime.worker is not None and runtime.worker.is_alive():
                    runtime.worker.terminate()
                    runtime.worker.join(timeout=5.0)

        ended_at = self._time_provider()
        report = {
            "generated_at": ended_at.isoformat(),
            "phase": "PAPER_LIVE_SUPERVISOR",
            "summary": {
                "status": status,
                "campaign_id": cfg.campaign_id,
                "strategy": cfg.strategy_name,
                "version": cfg.strategy_version,
                "contexts": len(runtimes),
                "supervision_cycles": cycles,
                "events": len(summary_events),
                "permanent_failures": len([r for r in runtimes.values() if r.permanent_failure]),
                "total_restarts": sum(int(r.total_restarts) for r in runtimes.values()),
                "audit_jsonl": str(audit_jsonl),
            },
            "contexts": [
                {
                    "symbol": runtime.context.symbol,
                    "timeframe": runtime.context.timeframe,
                    "consecutive_restarts": runtime.consecutive_restarts,
                    "total_restarts": runtime.total_restarts,
                    "permanent_failure": runtime.permanent_failure,
                    "campaign_id": runtime.launch_cfg.campaign_id,
                    "resume": runtime.launch_cfg.resume,
                    "has_hypothesis_config": isinstance(runtime.launch_cfg.hypothesis_config, dict),
                }
                for runtime in runtimes.values()
            ],
            "events": summary_events,
        }

        out_json = self._results_dir / f"paper_live_supervisor_{stamp}.json"
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "summary": report["summary"],
            "report": report,
            "outputs": {
                "json": str(out_json),
                "audit_jsonl": str(audit_jsonl),
            },
        }

    def _restart_context(
        self,
        runtime: _ContextRuntime,
        *,
        cfg: PaperLiveSupervisorConfig,
        now: datetime,
        audit_jsonl: Path,
        summary_events: list[dict[str, Any]],
        reason: str,
    ) -> None:
        if runtime.consecutive_restarts >= int(max(1, cfg.max_consecutive_restarts)):
            runtime.permanent_failure = True
            self._append_event(
                audit_jsonl,
                summary_events,
                runtime,
                reason="RESTART_LIMIT_REACHED",
                action="MARK_PERMANENT_FAILURE",
                now=now,
            )
            return

        if runtime.worker is not None and runtime.worker.is_alive():
            runtime.worker.terminate()
            runtime.worker.join(timeout=5.0)

        runtime.consecutive_restarts += 1
        runtime.total_restarts += 1

        self._append_event(
            audit_jsonl,
            summary_events,
            runtime,
            reason=reason,
            action="RESTARTING",
            now=now,
        )

        if cfg.restart_delay_seconds > 0:
            self._sleeper(float(cfg.restart_delay_seconds))

        self._start_context(runtime, audit_jsonl, summary_events, reason=reason)

    def _start_context(
        self,
        runtime: _ContextRuntime,
        audit_jsonl: Path,
        summary_events: list[dict[str, Any]],
        *,
        reason: str,
    ) -> None:
        now = self._time_provider()
        try:
            runtime.worker = self._launcher(runtime.launch_cfg, self._base_dir)
            runtime.started_at = now
            runtime.last_progress_at = now
            runtime.last_progress_token = None
            runtime.permanent_failure = False
            self._append_event(
                audit_jsonl,
                summary_events,
                runtime,
                reason=reason,
                action="STARTED",
                now=now,
            )
        except Exception as exc:  # pragma: no cover - defensive
            runtime.worker = None
            self._append_event(
                audit_jsonl,
                summary_events,
                runtime,
                reason=reason,
                action="START_FAILED",
                now=now,
                extra={"error": str(exc)},
            )

    def _append_event(
        self,
        audit_jsonl: Path,
        summary_events: list[dict[str, Any]],
        runtime: _ContextRuntime,
        *,
        reason: str,
        action: str,
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "at": now.isoformat(),
            "symbol": runtime.context.symbol,
            "timeframe": runtime.context.timeframe,
            "reason": reason,
            "action": action,
            "consecutive_restarts": runtime.consecutive_restarts,
            "total_restarts": runtime.total_restarts,
            "campaign_id": runtime.launch_cfg.campaign_id,
        }
        if extra:
            payload.update(extra)
        summary_events.append(payload)
        with audit_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _resolve_contexts(self, cfg: PaperLiveSupervisorConfig) -> list[EdgeDriftContext]:
        if cfg.contexts:
            return list(cfg.contexts)
        if not cfg.contexts_from_latest_report:
            return []

        reports = sorted(self._results_dir.glob("paper_specialized_campaign_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in reports:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
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

    @staticmethod
    def _context_key(ctx: EdgeDriftContext) -> str:
        return f"{ctx.symbol}::{ctx.timeframe}"

    @staticmethod
    def _default_launcher(launch_cfg: PaperLiveLaunchConfig, base_dir: Path) -> WorkerHandle:
        process = Process(target=_paper_live_worker_entrypoint, args=(str(base_dir), asdict(launch_cfg)))
        process.start()
        return ProcessWorkerHandle(process)

    @staticmethod
    def _default_progress_reader(results_dir: Path, launch_cfg: PaperLiveLaunchConfig) -> SupervisorProgressSnapshot | None:
        state_key = PaperLiveService._state_key(launch_cfg.strategy_name, launch_cfg.symbol, launch_cfg.timeframe)
        state_file = results_dir / f"paper_live_state__{state_key}.json"
        if not state_file.exists():
            return None
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # On Windows, atomic replace/write can briefly lock the file.
            # Treat transient read failures as "no progress snapshot" and retry next cycle.
            return None
        if not isinstance(payload, dict):
            return None
        return SupervisorProgressSnapshot(
            cycles=PaperLiveSupervisorService._to_int(payload.get("cycles")),
            last_open_time=str(payload.get("last_open_time") or "") or None,
            updated_at=str(payload.get("updated_at") or "") or None,
        )

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def _paper_live_worker_entrypoint(base_dir: str, launch_cfg_dict: dict[str, Any]) -> None:
    cfg = PaperLiveConfig(
        symbol=str(launch_cfg_dict["symbol"]),
        timeframe=str(launch_cfg_dict["timeframe"]),
        strategy_name=str(launch_cfg_dict["strategy_name"]),
        strategy_version=str(launch_cfg_dict["strategy_version"]),
        campaign_id=str(launch_cfg_dict.get("campaign_id") or "") or None,
        initial_capital=float(launch_cfg_dict["initial_capital"]),
        poll_seconds=float(launch_cfg_dict["poll_seconds"]),
        bootstrap_bars=int(launch_cfg_dict["bootstrap_bars"]),
        bootstrap_replay_bars=int(launch_cfg_dict["bootstrap_replay_bars"]),
        max_cycles=0,
        resume=bool(launch_cfg_dict.get("resume", True)),
        min_trades_before_change=int(launch_cfg_dict["min_trades_before_change"]),
        output_prefix=str(launch_cfg_dict["output_prefix"]),
        hypothesis_config=(dict(launch_cfg_dict.get("hypothesis_config")) if isinstance(launch_cfg_dict.get("hypothesis_config"), dict) else None),
    )
    service = PaperLiveService(base_dir=Path(base_dir))
    service.run(cfg)
