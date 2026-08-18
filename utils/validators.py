"""
Input validation utilities used at system boundaries (API responses, user
inputs, configuration).  Pure functions; no side effects.
"""
from __future__ import annotations

from typing import Any


_KNOWN_QUOTES = (
    "USDT",
    "USDC",
    "BUSD",
    "USD",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
)


def normalize_symbol(symbol: str) -> str:
    """Normalize symbol variants to canonical BASE/QUOTE format.

    Examples:
        BTCUSDT -> BTC/USDT
        BTC-USDT -> BTC/USDT
        btc/usdt -> BTC/USDT
    """
    value = symbol.strip().upper().replace("-", "/")
    if "/" in value:
        base, quote = value.split("/", 1)
        if not base or not quote:
            raise ValueError(
                f"Invalid symbol '{symbol}'. Expected format: 'BASE/QUOTE' (e.g. 'BTC/USDT')."
            )
        return f"{base}/{quote}"

    for quote in _KNOWN_QUOTES:
        if value.endswith(quote) and len(value) > len(quote):
            base = value[: -len(quote)]
            if base:
                return f"{base}/{quote}"

    raise ValueError(
        f"Invalid symbol '{symbol}'. Expected format: 'BASE/QUOTE' (e.g. 'BTC/USDT')."
    )


def normalize_timeframe(timeframe: str) -> str:
    """Normalize timeframe aliases to canonical format used by the platform."""
    raw = timeframe.strip()
    if raw == "1M":
        return "1M"
    value = raw.lower()
    aliases = {
        "05m": "5m",
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "60m": "1h",
        "1hr": "1h",
        "4hr": "4h",
    }
    return aliases.get(value, value)


def validate_positive_float(value: Any, name: str) -> float:
    """
    Ensure *value* can be cast to a positive float.

    Args:
        value: The value to validate.
        name: Human-readable field name used in error messages.

    Returns:
        The validated float value.

    Raises:
        TypeError: If *value* cannot be cast to float.
        ValueError: If *value* is not positive (> 0).
    """
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"'{name}' must be a number, got {type(value).__name__}.") from exc
    if result <= 0:
        raise ValueError(f"'{name}' must be positive (> 0), got {result}.")
    return result


def validate_percentage(value: Any, name: str) -> float:
    """
    Ensure *value* is a float in the (0, 1] range.

    Args:
        value: The value to validate.
        name: Human-readable field name used in error messages.

    Returns:
        The validated percentage as a float.

    Raises:
        ValueError: If *value* is outside the (0, 1] range.
    """
    result = validate_positive_float(value, name)
    if result > 1.0:
        raise ValueError(
            f"'{name}' must be a fraction in (0, 1], got {result}. "
            "Did you mean to divide by 100?"
        )
    return result


def validate_symbol(symbol: str) -> str:
    """
    Validate and normalize a trading pair symbol (e.g. ``BTC/USDT``).

    Args:
        symbol: Trading symbol string.

    Returns:
        Upper-cased, stripped symbol.

    Raises:
        ValueError: If the symbol format is invalid.
    """
    symbol = normalize_symbol(symbol)
    if "/" not in symbol or len(symbol) < 5:
        raise ValueError(
            f"Invalid symbol '{symbol}'. Expected format: 'BASE/QUOTE' (e.g. 'BTC/USDT')."
        )
    return symbol


def validate_timeframe(timeframe: str) -> str:
    """
    Validate that a timeframe string is a recognized ccxt/Binance value.

    Args:
        timeframe: Timeframe string (e.g. ``1m``, ``1h``, ``1d``).

    Returns:
        The validated timeframe string.

    Raises:
        ValueError: If the timeframe is not recognized.
    """
    timeframe = normalize_timeframe(timeframe)
    valid_timeframes = {
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    }
    if timeframe not in valid_timeframes:
        raise ValueError(
            f"Invalid timeframe '{timeframe}'. "
            f"Valid options: {sorted(valid_timeframes)}"
        )
    return timeframe
