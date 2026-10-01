"""
Funções auxiliares de uso geral.

Decisão de projeto: somente funções puras e sem estado. Sem efeitos colaterais
ou estado global. Cada função auxiliar se concentra em uma única transformação
ou validação.
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
    """Retorna a data/hora atual em UTC (com fuso horário)."""
    return datetime.now(tz=timezone.utc)


def timestamp_to_datetime(ts_ms: int) -> datetime:
    """
    Converte um timestamp Unix em milissegundos para uma data/hora UTC.

    Args:
        ts_ms: Timestamp Unix em milissegundos.

    Returns:
        Data/hora UTC com fuso horário.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def datetime_to_timestamp_ms(dt: datetime) -> int:
    """
    Converte uma data/hora em um timestamp Unix em milissegundos.

    Args:
        dt: Objeto de data/hora (datas sem fuso são tratadas como UTC).

    Returns:
        Timestamp Unix em milissegundos.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Utilitarios de DataFrame
# ---------------------------------------------------------------------------


def validate_ohlcv_dataframe(df: pd.DataFrame) -> None:
    """
    Verifica se um DataFrame contém as colunas OHLCV obrigatórias.

    Args:
        df: DataFrame a validar.

    Raises:
        ValueError: se alguma coluna obrigatória estiver ausente.
    """
    required_columns = {"open", "high", "low", "close", "volume"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")


def normalize_ohlcv_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza um DataFrame OHLCV para garantir tipos e índice consistentes.

    - Converte o índice para DatetimeIndex com fuso horário UTC.
    - Converte as colunas OHLCV para float64.
    - Ordena os timestamps em ordem crescente.
    - Remove índices duplicados.

    Args:
        df: DataFrame OHLCV bruto.

    Returns:
        Cópia normalizada do DataFrame.
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
    Decorador que registra em log o tempo de execução da função decorada.

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
    Decorador que tenta novamente executar a função decorada em caso de exceção.

    Args:
        max_attempts: Número máximo de tentativas antes de propagar novamente a exceção.
        delay_seconds: Segundos de espera entre as tentativas.

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
