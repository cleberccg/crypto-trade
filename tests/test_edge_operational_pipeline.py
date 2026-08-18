from __future__ import annotations

from pathlib import Path

from main import _parse_args
from research.services.edge_external_validation_lab import _confidence_score, _extract_owner_repo, _paper_status, _status_reason
from research.services.edge_operational_pipeline import EdgeOperationalPipelineConfig, EdgeOperationalPipelineService


def test_cli_parser_edge_external_validation_lab(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-external-validation-lab",
            "--edge01-report-file",
            "optimization/results/edge_extraction_lab_20260702_120000.json",
            "--min-external-candidates",
            "5",
            "--max-external-candidates",
            "8",
            "--strict-web-filters",
            "--min-repo-stars",
            "40",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-external-validation-lab"
    assert args.min_external_candidates == 5
    assert args.max_external_candidates == 8
    assert args.strict_web_filters is True
    assert args.min_repo_stars == 40


def test_cli_parser_edge_operational_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-operational-pipeline",
            "--prioritized-strategies",
            "Ichimoku Kumo Breakout,ClassicDonchianBreakout,ClassicATRBreakout",
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--timeframes",
            "5m,1h",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-operational-pipeline"
    assert args.symbols == "BTC/USDT,ETH/USDT"
    assert args.timeframes == "5m,1h"


def test_cli_parser_edge_operational_pipeline_new_args(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "edge-operational-pipeline",
            "--strategy-version",
            "v2.3",
            "--default-platform-strategy",
            "ClassicATRBreakout",
        ],
    )
    args = _parse_args()
    assert args.command == "edge-operational-pipeline"
    assert args.strategy_version == "v2.3"
    assert args.default_platform_strategy == "ClassicATRBreakout"


def test_edge_operational_pipeline_contract_and_status_progression(monkeypatch, tmp_path: Path) -> None:
    service = EdgeOperationalPipelineService(tmp_path)
    sequence: list[str] = []

    def fake_discovery(_cfg: EdgeOperationalPipelineConfig) -> dict:
        sequence.append("discovery")
        return {
            "summary": {},
            "report": {"decision": {"ranking": [{"strategy": "ClassicDonchianBreakout"}]}},
            "outputs": {"json": str(tmp_path / "discovery.json")},
        }

    def fake_extraction(_cfg: EdgeOperationalPipelineConfig) -> dict:
        sequence.append("extraction")
        extraction_json = tmp_path / "edge_extraction_lab_test.json"
        extraction_json.write_text("{}", encoding="utf-8")
        return {
            "summary": {"selected_strategies": ["ClassicDonchianBreakout"]},
            "report": {
                "candidate_filters": [
                    {"rule": "distance_to_ema_pct<=0.162026"},
                    {"rule": "atr_pct<=0.05"},
                ]
            },
            "outputs": {"json": str(extraction_json)},
        }

    def fake_external(_cfg: EdgeOperationalPipelineConfig, _json_file: str) -> dict:
        sequence.append("external")
        return {"summary": {}, "outputs": {"json": str(tmp_path / "external.json")}}

    def fake_router(_cfg: EdgeOperationalPipelineConfig) -> dict:
        sequence.append("router")
        return {
            "summary": {"conclusion": "router_preferred"},
            "report": {
                "router_map": [
                    {
                        "symbol": "BTC/USDT",
                        "timeframe": "5m",
                        "trend_bucket": "bullish",
                        "vol_regime": "normal_volatility",
                        "recommended_platform_strategy": "ClassicDonchianBreakout",
                        "score": 82.5,
                    }
                ]
            },
            "outputs": {"json": str(tmp_path / "router.json")},
        }

    def fake_backtest(_cfg: EdgeOperationalPipelineConfig, _contract) -> dict:
        sequence.append("backtest")
        return {"ok": True, "metrics": {"profit_factor": 1.4}}

    def fake_walk_forward(_cfg: EdgeOperationalPipelineConfig, _contract) -> dict:
        sequence.append("walk_forward")
        return {
            "passed": True,
            "best_parameters": {"breakout_period": 20},
            "best_validation_metrics": {
                "profit_factor": 1.5,
                "sharpe_ratio": 0.3,
                "expectancy": 0.1,
                "max_drawdown_pct": 0.08,
                "win_rate": 0.52,
            },
        }

    def fake_specialized_validation(_cfg: EdgeOperationalPipelineConfig, _contract, _wf: dict) -> dict:
        sequence.append("specialized_validation")
        return {
            "summary": {"verdict": "SIM", "classification": "PAPER_APPROVED_SPECIALIZED"},
            "report": {"final_answer": {"answer": "SIM"}},
            "outputs": {"json": str(tmp_path / "specialized_validation.json")},
        }

    def fake_specialized_campaign(_cfg: EdgeOperationalPipelineConfig, _contract, _validation: dict) -> dict:
        sequence.append("specialized_campaign")
        return {
            "summary": {"answer": "SIM", "final_status": "PAPER_APPROVED_SPECIALIZED"},
            "report": {"decision": {"answer": "SIM", "final_status": "PAPER_APPROVED_SPECIALIZED"}},
            "outputs": {"json": str(tmp_path / "specialized_campaign.json")},
        }

    monkeypatch.setattr(service, "_run_edge_discovery", fake_discovery)
    monkeypatch.setattr(service, "_run_edge_extraction", fake_extraction)
    monkeypatch.setattr(service, "_run_external_validation", fake_external)
    monkeypatch.setattr(service, "_run_market_router", fake_router)
    monkeypatch.setattr(service, "_run_backtest", fake_backtest)
    monkeypatch.setattr(service, "_run_walk_forward", fake_walk_forward)
    monkeypatch.setattr(service, "_run_specialized_validation", fake_specialized_validation)
    monkeypatch.setattr(service, "_run_specialized_campaign", fake_specialized_campaign)

    result = service.run(EdgeOperationalPipelineConfig())

    assert sequence == [
        "discovery",
        "extraction",
        "external",
        "router",
        "backtest",
        "walk_forward",
        "specialized_validation",
        "specialized_campaign",
    ]

    contract = result.get("contract", {})
    assert contract.get("strategy_name") == "ClassicDonchianBreakout"
    assert contract.get("strategy_version") == "v1.0"
    assert contract.get("symbols") == ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    assert contract.get("timeframes") == ("5m", "15m", "1h")
    assert contract.get("regime") == "router_preferred"
    assert contract.get("approved_filters") == ("distance_to_ema_pct<=0.162026", "atr_pct<=0.05")
    assert contract.get("approved_parameters") == {"breakout_period": 20}
    assert contract.get("hypothesis_status") == "PAPER_SPECIALIZED_CAMPAIGN_COMPLETED"

    status_history = contract.get("status_history", [])
    assert "DISCOVERED" in status_history
    assert "EXTRACTED" in status_history
    assert "ROUTED" in status_history
    assert "BACKTEST_COMPLETED" in status_history
    assert "WALK_FORWARD_APPROVED" in status_history
    assert "PAPER_SPECIALIZED_VALIDATED" in status_history
    assert "PAPER_SPECIALIZED_CAMPAIGN_COMPLETED" in status_history


def test_paper_status_approved_when_all_criteria_pass() -> None:
    status, reasons = _paper_status(
        {
            "profit_factor": 1.40,
            "sharpe": 0.20,
            "expectancy": 0.15,
            "return_pct": 0.05,
            "drawdown_pct": 0.08,
            "asset_robustness": 0.60,
            "timeframe_robustness": 0.60,
            "rolling_oos_consistency": 72.0,
            "paper_experimental_count": 3,
        }
    )

    assert status == "PAPER_APPROVED"
    assert reasons == []


def test_paper_status_rejected_when_criteria_fail() -> None:
    status, reasons = _paper_status(
        {
            "profit_factor": 1.02,
            "sharpe": -0.05,
            "expectancy": -0.01,
            "return_pct": -0.01,
            "drawdown_pct": 0.30,
            "asset_robustness": 0.20,
            "timeframe_robustness": 0.25,
            "rolling_oos_consistency": 40.0,
            "paper_experimental_count": 0,
        }
    )

    assert status == "REPROVADA"
    assert "profit_factor_below_threshold" in reasons
    assert "rolling_oos_inconsistent" in reasons


def test_extract_owner_repo_from_github_url() -> None:
    assert _extract_owner_repo("https://github.com/freqtrade/freqtrade-strategies") == (
        "freqtrade",
        "freqtrade-strategies",
    )


def test_confidence_score_stays_in_0_100_range() -> None:
    score, breakdown = _confidence_score(
        perf={"number_of_campaigns": 24},
        robust={"mean_profit_factor": 1.4, "std_profit_factor": 0.2, "mean_sharpe": 0.3, "std_sharpe": 0.1},
        profile={"regime_rows_tested": 50, "asset_robustness": 0.6, "timeframe_robustness": 0.7},
        metrics={"asset_robustness": 0.6, "timeframe_robustness": 0.7},
    )

    assert 0.0 <= score <= 100.0
    assert set(breakdown.keys()) == {
        "trade_component",
        "oos_component",
        "stability_component",
        "sensitivity_component",
    }


def test_status_reason_labels_are_explicit() -> None:
    assert _status_reason("PAPER_APPROVED", []) == "Atendeu todos os criterios"
    assert _status_reason("PAPER_CANDIDATE", ["rolling_oos_inconsistent"]) == "Aguardando paper prolongado"
    assert "profit_factor_below_threshold" in _status_reason("REPROVADA", ["profit_factor_below_threshold"])


def test_cli_parser_paper_specialized_validation(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "paper-specialized-validation",
            "--strategy-name",
            "ClassicDonchianBreakout",
            "--contexts",
            "BNB/USDT:1h,SOL/USDT:1h",
            "--min-validation-days",
            "30",
            "--min-validation-trades",
            "100",
        ],
    )
    args = _parse_args()
    assert args.command == "paper-specialized-validation"
    assert args.strategy_name == "ClassicDonchianBreakout"
    assert args.contexts == "BNB/USDT:1h,SOL/USDT:1h"
    assert args.min_validation_days == 30
    assert args.min_validation_trades == 100
