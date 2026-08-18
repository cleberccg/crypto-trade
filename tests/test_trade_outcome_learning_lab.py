from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.history_models import TradeOutcomeLearningRun
from database.history_repositories import TradeOutcomeLearningRunRepository
from main import _parse_args
from research.labs.trade_outcome_learning_lab import (
    TradeOutcomeLearningConfig,
    TradeOutcomeLearningLab,
    compute_trade_outcome_score,
)


def _write_sample_events(csv_path: Path, rows: int = 240) -> None:
    base = pd.Timestamp("2024-01-01", tz="UTC")
    data = []
    for i in range(rows):
        direction = "BUY" if i % 2 == 0 else "SELL"
        ret = 0.012 if i % 3 != 0 else -0.007
        data.append(
            {
                "symbol": "BTCUSDT" if i % 5 else "ETHUSDT",
                "timeframe": "5m" if i % 4 else "15m",
                "open_time": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "duration_minutes": 20,
                "trend_score": 0.05 + (i % 10) * 0.01,
                "atr_pct": 0.02 + (i % 8) * 0.005,
                "distance_to_ema_pct": 0.03 + (i % 7) * 0.004,
                "relative_volume": 1.0 + (i % 6) * 0.2,
                "rsi_bucket": "mid" if i % 3 else "low",
                "atr_bucket": "low_atr" if i % 2 else "high_atr",
                "volume_bucket": "low_volume" if i % 4 else "high_volume",
                "bollinger_position": "inside_band",
                "direction": direction,
                "future_return": ret,
                "future_upside": max(ret, 0.0) + 0.01,
                "future_downside": min(ret, 0.0) - 0.01,
                "regime": "reversao" if i % 2 else "continuacao",
                "primary_regime": "reversao" if i % 2 else "continuacao",
                "primary_profile": "neutral",
            }
        )
    pd.DataFrame(data).to_csv(csv_path, index=False)


def test_trade_outcome_score_range() -> None:
    score = compute_trade_outcome_score(
        expected_profit_factor=1.55,
        expected_expectancy=0.012,
        expected_sharpe=1.10,
        temporal_robustness=0.70,
        asset_robustness=0.65,
        regime_robustness=0.68,
        timeframe_robustness=0.60,
        generalization_score=0.72,
        simplicity_score=1.0,
        coverage_score=0.52,
    )
    assert 0.0 <= score <= 100.0
    assert score > 50.0


def test_cli_parser_trade_outcome_learning(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "trade-outcome-learning",
            "--targets",
            "winner,return_above",
            "--top-k-candidates",
            "12",
        ],
    )
    args = _parse_args()
    assert args.command == "trade-outcome-learning"
    assert args.targets == "winner,return_above"
    assert args.top_k_candidates == 12


def test_repository_trade_outcome_learning_upsert() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    TradeOutcomeLearningRun.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    with SessionLocal() as session:
        repo = TradeOutcomeLearningRunRepository(session)
        first = TradeOutcomeLearningRun(
            run_id="run-1",
            status="completed",
            decision="REJECT_IMPLEMENTATION",
            approved=False,
            target_name="winner",
            rule_text="trend_score>=0.1",
            trade_outcome_score=61.0,
            expected_profit_factor=1.1,
            expected_expectancy=0.002,
            expected_sharpe=0.5,
            temporal_robustness=0.5,
            asset_robustness=0.5,
            regime_robustness=0.5,
            timeframe_robustness=0.5,
            generalization_score=0.5,
            simplicity_score=1.0,
            coverage_score=0.3,
            overfit_flag=False,
            rejection_reason="low_score",
            artifacts_json="{}",
            summary_json="{}",
        )
        repo.save(first)

        second = TradeOutcomeLearningRun(
            run_id="run-1",
            status="completed",
            decision="APPROVE_IMPLEMENTATION",
            approved=True,
            target_name="winner",
            rule_text="trend_score>=0.2",
            trade_outcome_score=78.0,
            expected_profit_factor=1.6,
            expected_expectancy=0.01,
            expected_sharpe=1.2,
            temporal_robustness=0.7,
            asset_robustness=0.7,
            regime_robustness=0.7,
            timeframe_robustness=0.7,
            generalization_score=0.7,
            simplicity_score=1.0,
            coverage_score=0.4,
            overfit_flag=False,
            rejection_reason="",
            artifacts_json="{}",
            summary_json="{}",
        )
        saved = repo.save(second)
        assert saved.run_id == "run-1"
        assert saved.approved is True
        assert saved.trade_outcome_score == 78.0


def test_trade_outcome_learning_generates_artifacts(tmp_path: Path) -> None:
    events_file = tmp_path / "events_sample.csv"
    _write_sample_events(events_file, rows=260)

    service = TradeOutcomeLearningLab(session=None, base_dir=tmp_path)
    result = service.run(
        TradeOutcomeLearningConfig(
            events_glob="events_sample.csv",
            top_k_candidates=8,
            persist_to_db=False,
            output_prefix="trade_outcome_learning_test",
        )
    )

    outputs = result["outputs"]
    assert Path(outputs["json"]).exists()
    assert Path(outputs["csv"]).exists()
    assert Path(outputs["md"]).exists()
    assert result["summary"]["status"] == "COMPLETED"


def test_docs_workflow_guide_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs" / "WORKFLOW_GUIDE.md").exists()
    assert "trade-outcome-learning" in (root / "docs" / "PLATFORM_FEATURES.md").read_text(encoding="utf-8")
    assert "trade-outcome-learning" in (root / "docs" / "PLATFORM_MAP.md").read_text(encoding="utf-8")
