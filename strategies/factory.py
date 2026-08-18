"""Strategy factory used by optimizer, validation, backtesting and paper flows."""
from __future__ import annotations

from typing import Any

from strategies.base_strategy import BaseStrategy
from strategies.registry import filter_supported_kwargs, get_registration, normalize_parameters


def create_strategy(strategy_name: str, **parameters: Any) -> BaseStrategy:
    registration = get_registration(strategy_name)
    normalized = normalize_parameters(registration, parameters)
    filtered = filter_supported_kwargs(registration.strategy_cls, normalized)
    return registration.strategy_cls(**filtered)