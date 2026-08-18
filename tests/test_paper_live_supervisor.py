from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from paper_trading.edge_drift_monitor import EdgeDriftContext
from paper_trading.paper_live_supervisor import (
    PaperLiveLaunchConfig,
    PaperLiveSupervisorConfig,
    PaperLiveSupervisorService,
    SupervisorProgressSnapshot,
)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=max(0.0, float(seconds)))


@dataclass
class FakeWorker:
    alive_checks_before_exit: int = 999999

    def is_alive(self) -> bool:
        if self.alive_checks_before_exit <= 0:
            return False
        self.alive_checks_before_exit -= 1
        return True

    def terminate(self) -> None:
        self.alive_checks_before_exit = 0

    def join(self, timeout: float | None = None) -> None:
        _ = timeout


class FakeLauncher:
    def __init__(self, plans: dict[str, list[int]] | None = None, fail_on_start: dict[str, int] | None = None) -> None:
        self.plans = plans or {}
        self.fail_on_start = fail_on_start or {}
        self.starts: list[PaperLiveLaunchConfig] = []

    def __call__(self, cfg: PaperLiveLaunchConfig, _base_dir: Path) -> FakeWorker:
        key = f"{cfg.symbol}::{cfg.timeframe}"
        self.starts.append(cfg)

        remaining_failures = int(self.fail_on_start.get(key, 0))
        if remaining_failures > 0:
            self.fail_on_start[key] = remaining_failures - 1
            raise RuntimeError(f"simulated start failure for {key}")

        plan = self.plans.get(key, [999999])
        if plan:
            alive_checks = plan.pop(0)
        else:
            alive_checks = 999999
        return FakeWorker(alive_checks_before_exit=alive_checks)


class FakeProgressReader:
    def __init__(self, plans: dict[str, list[tuple[int | None, str | None, str | None] | None]]) -> None:
        self.plans = plans

    def __call__(self, _results_dir: Path, cfg: PaperLiveLaunchConfig) -> SupervisorProgressSnapshot | None:
        key = f"{cfg.symbol}::{cfg.timeframe}"
        plan = self.plans.get(key, [])
        if not plan:
            return None
        item = plan.pop(0)
        if item is None:
            return None
        cycles, last_open_time, updated_at = item
        return SupervisorProgressSnapshot(cycles=cycles, last_open_time=last_open_time, updated_at=updated_at)


def _cfg(*, contexts: tuple[EdgeDriftContext, ...], campaign_id: str = "spc-official-cdb-v1") -> PaperLiveSupervisorConfig:
    return PaperLiveSupervisorConfig(
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        campaign_id=campaign_id,
        contexts=contexts,
        contexts_from_latest_report=False,
        supervisor_poll_seconds=1.0,
        stuck_timeout_seconds=3.0,
        startup_grace_seconds=0.0,
        restart_delay_seconds=0.0,
        max_consecutive_restarts=3,
        max_supervision_cycles=6,
    )


def test_process_ended_is_restarted_automatically(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="ETH/USDT", timeframe="5m")
    launcher = FakeLauncher(plans={"ETH/USDT::5m": [1, 999999]})
    progress = FakeProgressReader(plans={"ETH/USDT::5m": [(1, "t1", "u1"), (2, "t2", "u2"), (3, "t3", "u3")]})
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    result = service.run(_cfg(contexts=(ctx,)))

    assert len(launcher.starts) >= 2
    assert result["summary"]["total_restarts"] >= 1


def test_stuck_process_is_restarted(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")
    launcher = FakeLauncher(plans={"BTC/USDT::5m": [999999, 999999]})
    progress = FakeProgressReader(
        plans={
            "BTC/USDT::5m": [
                (1, "t1", "u1"),
                (1, "t1", "u1"),
                (1, "t1", "u1"),
                (1, "t1", "u1"),
                (2, "t2", "u2"),
            ]
        }
    )
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    result = service.run(_cfg(contexts=(ctx,)))

    assert len(launcher.starts) >= 2
    events = result["report"]["events"]
    assert any(event["reason"] == "STUCK_NO_PROGRESS" for event in events)


def test_campaign_id_is_preserved_on_restarts(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="SOL/USDT", timeframe="15m")
    launcher = FakeLauncher(plans={"SOL/USDT::15m": [1, 1, 999999]})
    progress = FakeProgressReader(plans={"SOL/USDT::15m": [(1, "t1", "u1"), (2, "t2", "u2")]})
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    campaign_id = "spc-official-cdb-v1"
    service.run(_cfg(contexts=(ctx,), campaign_id=campaign_id))

    assert len(launcher.starts) >= 2
    assert all(start.campaign_id == campaign_id for start in launcher.starts)


def test_restore_is_enabled_after_restart(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="BNB/USDT", timeframe="5m")
    launcher = FakeLauncher(plans={"BNB/USDT::5m": [1, 999999]})
    progress = FakeProgressReader(plans={"BNB/USDT::5m": [(1, "t1", "u1"), (2, "t2", "u2")]})
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    service.run(_cfg(contexts=(ctx,)))

    assert len(launcher.starts) >= 2
    assert all(start.resume is True for start in launcher.starts)


def test_hypothesis_payload_is_preserved_after_restart(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="ADA/USDT", timeframe="5m")
    launcher = FakeLauncher(plans={"ADA/USDT::5m": [1, 999999]})
    progress = FakeProgressReader(plans={"ADA/USDT::5m": [(1, "t1", "u1"), (2, "t2", "u2")]})
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    hypothesis_payload = {
        "approved_parameters": {"entry_step": 5},
        "approved_filters": ["gate_flag >= 1"],
        "regime": "bullish|high_volatility",
        "approved_contexts": [{"symbol": "ADA/USDT", "timeframe": "5m"}],
    }
    cfg = _cfg(contexts=(ctx,))
    cfg = PaperLiveSupervisorConfig(**{**cfg.__dict__, "hypothesis_config": hypothesis_payload})

    service.run(cfg)

    assert len(launcher.starts) >= 2
    assert all(start.hypothesis_config == hypothesis_payload for start in launcher.starts)


def test_execution_continues_with_trade_progress_after_restart(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="BTC/USDT", timeframe="15m")
    launcher = FakeLauncher(plans={"BTC/USDT::15m": [1, 999999]})
    progress = FakeProgressReader(
        plans={
            "BTC/USDT::15m": [
                (10, "t1", "u1"),
                (10, "t1", "u1"),
                (11, "t2", "u2"),
                (12, "t3", "u3"),
            ]
        }
    )
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    result = service.run(_cfg(contexts=(ctx,)))

    context_report = result["report"]["contexts"][0]
    assert context_report["permanent_failure"] is False
    assert context_report["total_restarts"] >= 1


def test_multiple_contexts_are_supervised_independently(tmp_path: Path) -> None:
    c1 = EdgeDriftContext(symbol="ETH/USDT", timeframe="1h")
    c2 = EdgeDriftContext(symbol="SOL/USDT", timeframe="1h")
    launcher = FakeLauncher(
        plans={
            "ETH/USDT::1h": [1, 999999],
            "SOL/USDT::1h": [999999],
        }
    )
    progress = FakeProgressReader(
        plans={
            "ETH/USDT::1h": [(1, "t1", "u1"), (2, "t2", "u2")],
            "SOL/USDT::1h": [
                (1, "t1", "u1"),
                (2, "t2", "u2"),
                (3, "t3", "u3"),
                (4, "t4", "u4"),
                (5, "t5", "u5"),
                (6, "t6", "u6"),
            ],
        }
    )
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    result = service.run(_cfg(contexts=(c1, c2)))

    reports = {(item["symbol"], item["timeframe"]): item for item in result["report"]["contexts"]}
    assert reports[("ETH/USDT", "1h")]["total_restarts"] >= 1
    assert reports[("SOL/USDT", "1h")]["total_restarts"] == 0


def test_restart_limit_is_enforced(tmp_path: Path) -> None:
    ctx = EdgeDriftContext(symbol="ETH/USDT", timeframe="15m")
    launcher = FakeLauncher(plans={"ETH/USDT::15m": [0, 0, 0, 0]})
    progress = FakeProgressReader(plans={"ETH/USDT::15m": []})
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    cfg = _cfg(contexts=(ctx,))
    cfg = PaperLiveSupervisorConfig(**{**cfg.__dict__, "max_consecutive_restarts": 2, "max_supervision_cycles": 6})
    result = service.run(cfg)

    context_report = result["report"]["contexts"][0]
    assert context_report["permanent_failure"] is True
    assert context_report["consecutive_restarts"] >= 2


def test_permanent_failure_does_not_crash_supervisor(tmp_path: Path) -> None:
    c1 = EdgeDriftContext(symbol="ETH/USDT", timeframe="5m")
    c2 = EdgeDriftContext(symbol="BNB/USDT", timeframe="1h")
    launcher = FakeLauncher(
        plans={
            "ETH/USDT::5m": [0, 0, 0, 0],
            "BNB/USDT::1h": [999999],
        }
    )
    progress = FakeProgressReader(
        plans={
            "BNB/USDT::1h": [(1, "t1", "u1"), (2, "t2", "u2"), (3, "t3", "u3")],
        }
    )
    clock = FakeClock(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))

    service = PaperLiveSupervisorService(
        tmp_path,
        launcher=launcher,
        progress_reader=progress,
        time_provider=clock.now,
        sleeper=clock.sleep,
    )
    cfg = _cfg(contexts=(c1, c2))
    cfg = PaperLiveSupervisorConfig(**{**cfg.__dict__, "max_consecutive_restarts": 2, "max_supervision_cycles": 6})
    result = service.run(cfg)

    assert result["summary"]["status"] == "completed"
    assert result["summary"]["permanent_failures"] >= 1
