from __future__ import annotations

import pandas as pd

from research.services.scientific_robustness_validation import (
    INCONCLUSIVE_STATUS,
    classify_dataset,
    compute_scientific_robustness_score,
    is_trivial_rule,
    temporal_split_frame,
)


def test_temporal_split_frame_no_leakage() -> None:
    df = pd.DataFrame(
        {
            "open_time": pd.date_range("2024-01-01", periods=20, freq="D", tz="UTC"),
            "future_return": [0.01] * 20,
        }
    )

    train, val, test = temporal_split_frame(df, train_ratio=0.6, validation_ratio=0.2)

    assert len(train) > 0
    assert len(val) > 0
    assert len(test) > 0
    assert train["open_time"].max() < val["open_time"].min()
    assert val["open_time"].max() < test["open_time"].min()


def test_is_trivial_rule_by_coverage() -> None:
    assert is_trivial_rule(
        support_share=0.98,
        precision=0.60,
        base_rate=0.55,
        max_rule_coverage=0.95,
        min_discrimination_gap=0.04,
    )


def test_is_trivial_rule_by_low_discrimination() -> None:
    assert is_trivial_rule(
        support_share=0.50,
        precision=0.52,
        base_rate=0.50,
        max_rule_coverage=0.95,
        min_discrimination_gap=0.04,
    )


def test_scientific_score_range() -> None:
    score = compute_scientific_robustness_score(
        temporal_robustness=0.8,
        asset_robustness=0.7,
        regime_robustness=0.6,
        generalization_score=0.75,
        operational_edge_score=82.0,
        statistical_stability=0.65,
    )
    assert 0.0 <= score <= 100.0
    assert score > 60.0


def test_classify_dataset_full_dataset() -> None:
    result = classify_dataset(
        files=75,
        events=17_744_835,
        assets=10,
        timeframes=4,
        min_files=75,
        min_events=17_000_000,
        min_assets=10,
        min_timeframes=4,
    )
    assert result == "FULL_DATASET"


def test_classify_dataset_limited_sample() -> None:
    result = classify_dataset(
        files=40,
        events=9_000_000,
        assets=8,
        timeframes=3,
        min_files=75,
        min_events=17_000_000,
        min_assets=10,
        min_timeframes=4,
    )
    assert result == "LIMITED_SAMPLE"


def test_classify_dataset_insufficient_sample() -> None:
    result = classify_dataset(
        files=1,
        events=1000,
        assets=1,
        timeframes=1,
        min_files=75,
        min_events=17_000_000,
        min_assets=10,
        min_timeframes=4,
    )
    assert result == "INSUFFICIENT_SAMPLE"


def test_inconclusive_status_constant() -> None:
    assert INCONCLUSIVE_STATUS == "VALIDACAO_INCONCLUSIVA"
