"""
Utilitários de validação de entradas usados nos limites do sistema (respostas
da API, entradas de usuário e configuração). Funções puras, sem efeitos colaterais.
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
    """Normaliza variantes de símbolos para o formato canônico BASE/QUOTE.

        Exemplos:
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
    """Normaliza aliases de períodos para o formato canônico usado pela plataforma."""
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
    Garante que *value* possa ser convertido para um float positivo.

    Args:
        value: Valor a validar.
        name: Nome legível do campo usado nas mensagens de erro.

    Returns:
        O valor float validado.

    Raises:
        TypeError: se *value* não puder ser convertido para float.
        ValueError: se *value* não for positivo (> 0).
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
    Garante que *value* seja um float no intervalo (0, 1].

    Args:
        value: Valor a validar.
        name: Nome legível do campo usado nas mensagens de erro.

    Returns:
        O percentual validado como float.

    Raises:
        ValueError: se *value* estiver fora do intervalo (0, 1].
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
    Valida e normaliza o símbolo de um par de negociação (por exemplo, ``BTC/USDT``).

    Args:
        symbol: Texto com o símbolo de negociação.

    Returns:
        Símbolo sem espaços e em letras maiúsculas.

    Raises:
        ValueError: se o formato do símbolo for inválido.
    """
    symbol = normalize_symbol(symbol)
    if "/" not in symbol or len(symbol) < 5:
        raise ValueError(
            f"Invalid symbol '{symbol}'. Expected format: 'BASE/QUOTE' (e.g. 'BTC/USDT')."
        )
    return symbol


def validate_timeframe(timeframe: str) -> str:
    """
    Valida se o texto do período corresponde a um valor reconhecido pelo ccxt/Binance.

    Args:
        timeframe: Texto do período (por exemplo, ``1m``, ``1h``, ``1d``).

    Returns:
        O texto do período validado.

    Raises:
        ValueError: se o período não for reconhecido.
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
