from __future__ import annotations

from main import _parse_args
from paper_trading.edge_drift_monitor import EdgeDriftMonitorService, EdgeDriftThresholds, _alert_level, _ratio_score


def test_ratio_score_rewards_better_than_expected() -> None:
    assert _ratio_score(2.0, 3.0, True) == 100.0
    assert _ratio_score(2.0, 1.0, True) == 50.0


def test_alert_level_respects_thresholds() -> None:
    thresholds = EdgeDriftThresholds(
        attention_health_score=70.0,
        critical_health_score=50.0,
        attention_metric_degradation_pct=0.15,
        critical_metric_degradation_pct=0.30,
        attention_drawdown_worsening_pct=0.10,
        critical_drawdown_worsening_pct=0.25,
        attention_stability_score=70.0,
        critical_stability_score=50.0,
    )

    assert _alert_level({}, 82.0, 88.0, thresholds) == "NORMAL"
    assert _alert_level({}, 65.0, 88.0, thresholds) == "ATENCAO"
    assert _alert_level({}, 45.0, 88.0, thresholds) == "CRITICO"
    assert _alert_level({}, None, 88.0, thresholds) == "INSUFFICIENT_REFERENCE"


def test_health_components_all_references_missing_returns_na() -> None:
    service = EdgeDriftMonitorService(base_dir=__import__("pathlib").Path("d:/xampp/htdocs/crypto"))
    comparisons = {
        "profit_factor": {"expected": None, "observed": 1.2, "higher_is_better": True},
        "sharpe": {"expected": None, "observed": 0.4, "higher_is_better": True},
        "drawdown": {"expected": None, "observed": 0.1, "higher_is_better": False},
    }
    components = service._health_components(comparisons, 90.0)
    assert components["final_score"] is None
    assert components["available_metrics"] == 0
    assert components["coverage_pct"] == 0.0


def test_health_components_partial_references_use_only_available_metrics() -> None:
    service = EdgeDriftMonitorService(base_dir=__import__("pathlib").Path("d:/xampp/htdocs/crypto"))
    comparisons = {
        "profit_factor": {"expected": 2.0, "observed": 1.0, "higher_is_better": True},
        "sharpe": {"expected": None, "observed": 0.4, "higher_is_better": True},
        "drawdown": {"expected": 0.1, "observed": 0.2, "higher_is_better": False},
    }
    components = service._health_components(comparisons, 100.0)
    assert components["final_score"] is not None
    assert components["available_metrics"] == 2
    assert components["coverage_pct"] > 0.0


def test_health_components_full_references_compute_score() -> None:
    service = EdgeDriftMonitorService(base_dir=__import__("pathlib").Path("d:/xampp/htdocs/crypto"))
    comparisons = {
        "profit_factor": {"expected": 2.0, "observed": 2.0, "higher_is_better": True},
        "sharpe": {"expected": 1.0, "observed": 0.5, "higher_is_better": True},
        "expectancy": {"expected": 1.0, "observed": 1.0, "higher_is_better": True},
        "drawdown": {"expected": 0.1, "observed": 0.1, "higher_is_better": False},
        "win_rate": {"expected": 0.5, "observed": 0.5, "higher_is_better": True},
        "net_return": {"expected": 0.1, "observed": 0.1, "higher_is_better": True},
        "net_profit": {"expected": 10.0, "observed": 10.0, "higher_is_better": True},
        "mfe": {"expected": 0.2, "observed": 0.2, "higher_is_better": True},
        "mae": {"expected": 0.1, "observed": 0.1, "higher_is_better": False},
    }
    components = service._health_components(comparisons, 80.0)
    assert components["final_score"] is not None
    assert components["available_metrics"] == 9
    assert components["coverage_pct"] == 100.0


def test_cli_parser_edge_drift_monitor(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-drift-monitor",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--campaign-id",
            "spc-test-001",
            "--contexts",
            "BTC/USDT:1h,ETH/USDT:1h",
            "--lookback-days",
            "14",
            "--min-validation-trades",
            "120",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-drift-monitor"
    assert args.strategy_name == "ClassicDonchianBreakout"
    assert args.campaign_id == "spc-test-001"
    assert args.contexts == "BTC/USDT:1h,ETH/USDT:1h"
    assert args.lookback_days == 14
    assert args.min_validation_trades == 120