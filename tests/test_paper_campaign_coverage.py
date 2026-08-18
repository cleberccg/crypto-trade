from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from main import _parse_args
from paper_trading.edge_drift_monitor import EdgeDriftContext
from paper_trading.paper_campaign_coverage_monitor import (
    PaperCampaignCoverageConfig,
    PaperCampaignCoverageService,
)
import paper_trading.paper_campaign_coverage_monitor as coverage_module


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_service(tmp_path: Path) -> PaperCampaignCoverageService:
    return PaperCampaignCoverageService(base_dir=tmp_path)


def test_cli_parser_paper_campaign_coverage(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "paper-campaign-coverage",
            "--campaign-id",
            "spc-official-cdb-v1",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--strategy-version",
            "v1.0",
            "--stale-minutes",
            "240",
            "--min-coverage-percent",
            "92",
            "--critical-coverage-percent",
            "80",
        ],
    )
    args = _parse_args()
    assert args.command == "paper-campaign-coverage"
    assert args.campaign_id == "spc-official-cdb-v1"
    assert args.strategy_name == "ClassicDonchianBreakout"
    assert args.strategy_version == "v1.0"
    assert args.stale_minutes == 240
    assert args.min_coverage_percent == 92.0
    assert args.critical_coverage_percent == 80.0


def test_campaign_without_processes_marks_contexts_stopped(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [
        EdgeDriftContext(symbol="BTC/USDT", timeframe="5m"),
        EdgeDriftContext(symbol="ETH/USDT", timeframe="5m"),
    ]

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: [])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60, min_coverage_percent=90.0))

    summary = result["summary"]
    assert summary["approved_contexts"] == 2
    assert summary["active_contexts"] == 0
    assert summary["coverage_percent"] == 0.0
    assert summary["coverage_sufficient"] == "NAO"

    statuses = {(row["symbol"], row["timeframe"]): row["status"] for row in result["report"]["contexts"]}
    assert statuses[("BTC/USDT", "5m")] == "STOPPED"
    assert statuses[("ETH/USDT", "5m")] == "STOPPED"


def test_campaign_partially_active_computes_partial_coverage(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [
        EdgeDriftContext(symbol="BTC/USDT", timeframe="5m"),
        EdgeDriftContext(symbol="ETH/USDT", timeframe="5m"),
    ]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=10)), "last_open_time": _iso(now - timedelta(minutes=8)), "cycles": 22}],
        ("ETH/USDT", "5m"): [{"execution_id": "exec-2", "updated_at": _iso(now - timedelta(minutes=200)), "last_open_time": _iso(now - timedelta(minutes=190)), "cycles": 11}],
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1", "exec-2"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}, "exec-2": {"status": "running"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60, min_coverage_percent=90.0))
    summary = result["summary"]

    assert summary["approved_contexts"] == 2
    assert summary["active_contexts"] == 1
    assert summary["coverage_percent"] == 50.0
    assert summary["coverage_ok"] is False

    by_context = {(row["symbol"], row["timeframe"]): row for row in result["report"]["contexts"]}
    assert by_context[("BTC/USDT", "5m")]["status"] == "ACTIVE"
    assert by_context[("ETH/USDT", "5m")]["status"] == "STALE"


def test_campaign_fully_active_has_full_coverage(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [
        EdgeDriftContext(symbol="BTC/USDT", timeframe="5m"),
        EdgeDriftContext(symbol="ETH/USDT", timeframe="5m"),
    ]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=6)), "last_open_time": _iso(now - timedelta(minutes=4)), "cycles": 31}],
        ("ETH/USDT", "5m"): [{"execution_id": "exec-2", "updated_at": _iso(now - timedelta(minutes=12)), "last_open_time": _iso(now - timedelta(minutes=9)), "cycles": 28}],
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1", "exec-2"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}, "exec-2": {"status": "running"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60, min_coverage_percent=90.0))

    assert result["summary"]["coverage_percent"] == 100.0
    assert result["summary"]["coverage_ok"] is True
    assert result["summary"]["coverage_sufficient"] == "SIM"


def test_process_stopped_classification(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": None, "last_open_time": None, "cycles": 10}]
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "finished"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60))
    assert result["report"]["contexts"][0]["status"] == "STOPPED"


def test_process_resumed_becomes_active(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=3)), "last_open_time": _iso(now - timedelta(minutes=3)), "cycles": 40}]
    }
    signals = {
        ("BTC/USDT", "5m"): {"signals": 1, "last_signal": _iso(now - timedelta(minutes=2)), "last_execution_id": "exec-1"}
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: signals)
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60))
    assert result["report"]["contexts"][0]["status"] == "ACTIVE"


def test_recent_updated_at_without_real_progress_is_not_active(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=1)), "last_open_time": None, "cycles": 1}]
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60))
    assert result["report"]["contexts"][0]["status"] == "STOPPED"


def test_coverage_formula_and_gaps(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [
        EdgeDriftContext(symbol="BTC/USDT", timeframe="5m"),
        EdgeDriftContext(symbol="ETH/USDT", timeframe="5m"),
        EdgeDriftContext(symbol="SOL/USDT", timeframe="5m"),
    ]

    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=4)), "last_open_time": _iso(now - timedelta(minutes=4)), "cycles": 10}],
        ("ETH/USDT", "5m"): [{"execution_id": "exec-2", "updated_at": _iso(now - timedelta(minutes=5)), "last_open_time": _iso(now - timedelta(minutes=5)), "cycles": 10}],
        ("SOL/USDT", "5m"): [{"execution_id": "exec-3", "updated_at": _iso(now - timedelta(minutes=90)), "last_open_time": _iso(now - timedelta(minutes=90)), "cycles": 10}],
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1", "exec-2", "exec-3"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}, "exec-2": {"status": "running"}, "exec-3": {"status": "running"}})
    monkeypatch.setattr(service, "_write_outputs", lambda output_prefix, report: {"json": "x.json", "md": "x.md", "csv": "x.csv"})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60, min_coverage_percent=80.0))
    summary = result["summary"]

    assert summary["coverage_percent"] == 66.666667
    assert summary["coverage_ok"] is False
    assert result["report"]["coverage"]["formula"] == "2/3"
    assert len(result["report"]["coverage"]["coverage_gaps"]) == 1


def test_report_files_are_generated(tmp_path, monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc)
    service = _make_service(tmp_path)
    contexts = [EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")]
    states = {
        ("BTC/USDT", "5m"): [{"execution_id": "exec-1", "updated_at": _iso(now - timedelta(minutes=1)), "last_open_time": _iso(now - timedelta(minutes=1)), "cycles": 99}]
    }

    monkeypatch.setattr(service, "_resolve_contexts", lambda cfg: contexts)
    monkeypatch.setattr(service, "_resolve_execution_ids", lambda cfg: ["exec-1"])
    monkeypatch.setattr(service, "_load_paper_live_states", lambda cfg, execution_ids: states)
    monkeypatch.setattr(service, "_fetch_trade_activity", lambda cfg, contexts, execution_ids: {("BTC/USDT", "5m"): {"trades": 3, "last_trade": _iso(now - timedelta(minutes=2)), "last_execution_id": "exec-1"}})
    monkeypatch.setattr(service, "_fetch_signal_activity", lambda cfg, contexts, execution_ids: {("BTC/USDT", "5m"): {"signals": 5, "last_signal": _iso(now - timedelta(minutes=1)), "last_execution_id": "exec-1"}})
    monkeypatch.setattr(service, "_fetch_execution_activity", lambda execution_ids: {"exec-1": {"status": "running"}})

    result = service.run(PaperCampaignCoverageConfig(campaign_id="spc-test", stale_minutes=60))

    json_path = Path(result["outputs"]["json"])
    md_path = Path(result["outputs"]["md"])
    csv_path = Path(result["outputs"]["csv"])

    assert json_path.exists()
    assert md_path.exists()
    assert csv_path.exists()

    csv_content = csv_path.read_text(encoding="utf-8")
    assert "symbol,timeframe,execution_id,status,last_activity,minutes_without_activity,trades,last_trade,last_signal" in csv_content


def test_read_only_queries_only_select(monkeypatch, tmp_path) -> None:
    service = _make_service(tmp_path)
    cfg = PaperCampaignCoverageConfig(campaign_id="spc-test")
    contexts = [EdgeDriftContext(symbol="BTC/USDT", timeframe="5m")]

    captured_sql: list[str] = []

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeSession:
        def execute(self, stmt, params=None):
            captured_sql.append(str(stmt))
            return FakeResult()

    class FakeSessionContext:
        def __enter__(self):
            return FakeSession()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(coverage_module, "get_session", lambda: FakeSessionContext())

    service._fetch_trade_activity(cfg, contexts, ["exec-1"])
    service._fetch_signal_activity(cfg, contexts, ["exec-1"])
    service._fetch_execution_activity(["exec-1"])

    assert captured_sql
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in captured_sql)
