"""
General-purpose helper functions.

Design decision: Stateless pure functions only. No side effects, no global
state. Each helper focuses on a single transformation or validation.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar

import pandas as pd

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Utilitarios de tempo
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def timestamp_to_datetime(ts_ms: int) -> datetime:
    """
    Convert a Unix timestamp in milliseconds to a UTC datetime.

    Args:
        ts_ms: Unix timestamp in milliseconds.

    Returns:
        Timezone-aware UTC datetime.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def datetime_to_timestamp_ms(dt: datetime) -> int:
    """
    Convert a datetime to a Unix timestamp in milliseconds.

    Args:
        dt: Datetime object (naive datetimes are treated as UTC).

    Returns:
        Unix timestamp in milliseconds.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Utilitarios de DataFrame
# ---------------------------------------------------------------------------


def validate_ohlcv_dataframe(df: pd.DataFrame) -> None:
    """
    Assert that a DataFrame has the required OHLCV columns.

    Args:
        df: DataFrame to validate.

    Raises:
        ValueError: If any required column is missing.
    """
    required_columns = {"open", "high", "low", "close", "volume"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")


def normalize_ohlcv_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize an OHLCV DataFrame to ensure consistent types and index.

    - Converts the index to UTC-aware DatetimeIndex.
    - Casts OHLCV columns to float64.
    - Sorts by timestamp ascending.
    - Drops duplicate indices.

    Args:
        df: Raw OHLCV DataFrame.

    Returns:
        Normalized copy of the DataFrame.
    """
    df = df.copy()

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = df[col].astype("float64")

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    return df


# ---------------------------------------------------------------------------
# Decoradores
# ---------------------------------------------------------------------------


def timeit(func: F) -> F:
    """
    Decorator that logs the execution time of the decorated function.

    Usage::

        @timeit
        def my_function():
            ...
    """
    from utils.logger import get_logger

    logger = get_logger(func.__module__)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.debug(
                "Function '%s' completed in %.4f seconds.", func.__qualname__, elapsed
            )
            return result
        except Exception:
            elapsed = time.perf_counter() - start
            logger.error(
                "Function '%s' raised an exception after %.4f seconds.",
                func.__qualname__,
                elapsed,
            )
            raise

    return wrapper  # type: ignore[return-value]


def retry(max_attempts: int = 3, delay_seconds: float = 1.0) -> Callable[[F], F]:
    """
    Decorator that retries the decorated function on exception.

    Args:
        max_attempts: Maximum number of attempts before re-raising.
        delay_seconds: Seconds to wait between attempts.

    Usage::

        @retry(max_attempts=3, delay_seconds=2.0)
        def fetch_data():
            ...
    """
    from utils.logger import get_logger

    def decorator(func: F) -> F:
        logger = get_logger(func.__module__)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception = RuntimeError("No attempts made")
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Attempt %d/%d for '%s' failed: %s",
                        attempt,
                        max_attempts,
                        func.__qualname__,
                        exc,
                    )
                    if attempt < max_attempts:
                        time.sleep(delay_seconds)
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator
