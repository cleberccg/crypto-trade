from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.history_models import TradeOutcomeImplementationRun
from database.history_repositories import TradeOutcomeImplementationRunRepository
from main import _parse_args
from research.services.trade_outcome_controlled_implementation import (
    TradeOutcomeControlledImplementationConfig,
    TradeOutcomeControlledImplementationService,
)
from strategies.trade_outcome_nextgen_v1 import TradeOutcomeNextGenV1Strategy


def _write_events(csv_path: Path) -> None:
    base = pd.Timestamp("2024-01-01", tz="UTC")
    rows = []
    for i in range(120):
        dist = 0.10 if i % 3 != 0 else 0.30
        if dist <= 0.162026:
            ret = 0.02 if i % 7 != 0 else -0.01
        else:
            ret = -0.01
        rows.append(
            {
                "open_time": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "symbol": "BTC/USDT",
                "timeframe": "5m",
                "distance_to_ema_pct": dist,
                "future_return": ret,
                "future_return_20": ret,
                "drawdown": -0.02,
            }
        )
    pd.DataFrame(rows).to_csv(csv_path, index=False)


def _write_candidate(csv_path: Path) -> None:
    pd.DataFrame(
        [
            {
                "target": "return_above",
                "rule": "distance_to_ema_pct<=0.162026",
                "support": 80,
                "trade_outcome_score": 89.03,
                "scientific_robustness_score": 91.35,
                "expected_profit_factor": 1.1653,
                "expected_sharpe": 49.8036,
                "expected_expectancy": 0.1000,
                "expected_drawdown": -0.20,
                "confidence": 0.54,
                "generalization_score": 0.98,
                "approved": True,
            }
        ]
    ).to_csv(csv_path, index=False)


def test_strategy_entry_rule_uses_exact_threshold() -> None:
    strategy = TradeOutcomeNextGenV1Strategy(distance_threshold=0.162026)
    strategy.initialize()

    frame = pd.DataFrame(
        {
            "close": [100.0, 100.0],
            "distance_to_ema_pct": [0.162026, 0.1621],
        },
        index=pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z"], utc=True),
    )

    signal_ok = strategy.entry_signal(frame.iloc[:1])
    signal_no = strategy.entry_signal(frame.iloc[:2])

    assert signal_ok.signal.value == "BUY"
    assert signal_no.signal.value == "HOLD"


def test_cli_parser_phase9_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "phase9-controlled-implementation",
            "--distance-threshold",
            "0.162026",
            "--skip-optimizer-validation",
            "--skip-research-labs",
        ],
    )
    args = _parse_args()
    assert args.command == "phase9-controlled-implementation"
    assert abs(args.distance_threshold - 0.162026) < 1e-12
    assert args.skip_optimizer_validation is True


def test_repository_trade_outcome_implementation_upsert() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    TradeOutcomeImplementationRun.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    with SessionLocal() as session:
        repo = TradeOutcomeImplementationRunRepository(session)
        first = TradeOutcomeImplementationRun(
            run_id="p9-run",
            status="completed",
            decision="OPCAO_B",
            strategy_name="TradeOutcomeNextGenV1",
            target_name="return_above",
            rule_text="distance_to_ema_pct<=0.162026",
            fidelity_precision=0.90,
            fidelity_recall=0.90,
            fidelity_f1=0.90,
            overlap_count=90,
            false_positives=5,
            false_negatives=5,
            expected_profit_factor=1.1,
            observed_profit_factor=1.0,
            expected_sharpe=2.0,
            observed_sharpe=1.5,
            expected_expectancy=0.1,
            observed_expectancy=0.08,
            expected_drawdown=-0.2,
            observed_drawdown=-0.22,
            artifacts_json="{}",
            summary_json="{}",
        )
        repo.save(first)

        second = TradeOutcomeImplementationRun(
            run_id="p9-run",
            status="completed",
            decision="OPCAO_A",
            strategy_name="TradeOutcomeNextGenV1",
            target_name="return_above",
            rule_text="distance_to_ema_pct<=0.162026",
            fidelity_precision=1.0,
            fidelity_recall=1.0,
            fidelity_f1=1.0,
            overlap_count=100,
            false_positives=0,
            false_negatives=0,
            expected_profit_factor=1.1,
            observed_profit_factor=1.2,
            expected_sharpe=2.0,
            observed_sharpe=2.1,
            expected_expectancy=0.1,
            observed_expectancy=0.11,
            expected_drawdown=-0.2,
            observed_drawdown=-0.18,
            artifacts_json="{}",
            summary_json="{}",
        )
        saved = repo.save(second)

        assert saved.decision == "OPCAO_A"
        assert saved.fidelity_f1 == 1.0


def test_phase9_service_generates_artifacts_without_optional_pipeline(tmp_path: Path) -> None:
    events_csv = tmp_path / "events.csv"
    candidate_csv = tmp_path / "trade_outcome_learning_manual.csv"
    _write_events(events_csv)
    _write_candidate(candidate_csv)

    service = TradeOutcomeControlledImplementationService(session=None, base_dir=tmp_path)
    result = service.run(
        TradeOutcomeControlledImplementationConfig(
            events_glob="events.csv",
            trade_outcome_csv=str(candidate_csv),
            run_optimizer_validation=False,
            run_research_labs=False,
            persist_to_db=False,
            output_prefix="phase9_test",
        )
    )

    assert result["summary"]["decision"] in {"OPCAO_A", "OPCAO_B"}
    assert result["summary"]["fidelity_f1"] >= 0.95
    assert Path(result["outputs"]["json"]).exists()
    assert Path(result["outputs"]["csv"]).exists()
    assert Path(result["outputs"]["md"]).exists()
