from __future__ import annotations

from datetime import datetime, timezone

import pytest

from validation.validator import OptimizationValidator, ValidationCriteria


@pytest.fixture
def validator() -> OptimizationValidator:
    return OptimizationValidator(
        ValidationCriteria(
            min_trades=5,
            min_profit_factor=1.0,
            max_drawdown_pct=30.0,
            min_win_rate_pct=40.0,
            min_expectancy=0.0,
            min_sharpe=0.0,
        )
    )


def test_prevalidation_passes_when_data_range_is_available(monkeypatch: pytest.MonkeyPatch, validator: OptimizationValidator) -> None:
    monkeypatch.setattr(
        OptimizationValidator,
        "_get_available_date_range",
        staticmethod(lambda _s, _t: (datetime(2026, 6, 3), datetime(2026, 6, 27))),
    )

    validator.validate_data_availability(
        "BTC/USDT",
        "5m",
        datetime(2026, 6, 3, tzinfo=timezone.utc),
        datetime(2026, 6, 20, tzinfo=timezone.utc),
        datetime(2026, 6, 21, tzinfo=timezone.utc),
        datetime(2026, 6, 27, tzinfo=timezone.utc),
    )


def test_prevalidation_fails_with_clear_message_for_missing_period(
    monkeypatch: pytest.MonkeyPatch,
    validator: OptimizationValidator,
) -> None:
    monkeypatch.setattr(
        OptimizationValidator,
        "_get_available_date_range",
        staticmethod(lambda _s, _t: (datetime(2026, 6, 3), datetime(2026, 6, 27))),
    )

    with pytest.raises(ValueError, match="Data range gap for train period") as exc_info:
        validator.validate_data_availability(
            "BTC/USDT",
            "5m",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 12, 31, tzinfo=timezone.utc),
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 12, 31, tzinfo=timezone.utc),
        )

    message = str(exc_info.value)
    assert "Requested:" in message
    assert "Available:" in message


def test_prevalidation_fails_for_missing_symbol_or_timeframe(validator: OptimizationValidator) -> None:
    with pytest.raises(ValueError):
        validator.validate_data_availability(
            "BTC",
            "5m",
            datetime(2026, 6, 3, tzinfo=timezone.utc),
            datetime(2026, 6, 20, tzinfo=timezone.utc),
            datetime(2026, 6, 21, tzinfo=timezone.utc),
            datetime(2026, 6, 27, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError):
        validator.validate_data_availability(
            "BTC/USDT",
            "05x",
            datetime(2026, 6, 3, tzinfo=timezone.utc),
            datetime(2026, 6, 20, tzinfo=timezone.utc),
            datetime(2026, 6, 21, tzinfo=timezone.utc),
            datetime(2026, 6, 27, tzinfo=timezone.utc),
        )


def test_prevalidation_fails_when_no_data_exists(monkeypatch: pytest.MonkeyPatch, validator: OptimizationValidator) -> None:
    monkeypatch.setattr(
        OptimizationValidator,
        "_get_available_date_range",
        staticmethod(lambda _s, _t: (None, None)),
    )

    with pytest.raises(ValueError, match="NO HISTORICAL DATA FOUND"):
        validator.validate_data_availability(
            "BTC/USDT",
            "5m",
            datetime(2026, 6, 3, tzinfo=timezone.utc),
            datetime(2026, 6, 20, tzinfo=timezone.utc),
            datetime(2026, 6, 21, tzinfo=timezone.utc),
            datetime(2026, 6, 27, tzinfo=timezone.utc),
        )
