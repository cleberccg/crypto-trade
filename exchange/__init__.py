"""Clientes de exchange carregados sob demanda, sem inicializar negociacao na auditoria."""
from importlib import import_module

__all__ = ["BaseExchange", "BinanceClient", "BinanceMarketDataClient"]
_MODULES = {
    "BaseExchange": "exchange.base_exchange",
    "BinanceClient": "exchange.binance_client",
    "BinanceMarketDataClient": "exchange.binance_market_data_client",
}


def __getattr__(name):
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
