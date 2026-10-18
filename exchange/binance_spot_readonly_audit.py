"""Cliente isolado para auditoria privada Binance Spot; expõe somente endpoints GET permitidos."""
from __future__ import annotations

import hashlib
import hmac
import os
from time import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse

import requests


_BASE_URL = "https://api.binance.com"
_ALLOWED_ROUTES = {
    "account": "/api/v3/account",
    "open_orders": "/api/v3/openOrders",
    "all_orders": "/api/v3/allOrders",
    "my_trades": "/api/v3/myTrades",
}
_ALLOWED_SYMBOL = "BNBUSDT"
_MAX_PAGE_SIZE = 1000
_MAX_PAGES = 100


class BinanceAuditConfigurationError(RuntimeError):
    """Configuração explícita e segregada de auditoria ausente ou inválida."""


class BinanceAuditRequestError(RuntimeError):
    """Falha HTTP/API sem conteúdo potencialmente sensível da resposta."""


class BinanceSpotReadOnlyAuditClient:
    """Leitor autenticado Spot restrito a quatro rotas fixas, todas via HTTP GET."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        transport: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key or not api_secret:
            raise BinanceAuditConfigurationError("Credenciais dedicadas de auditoria não configuradas.")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._clock_ms = clock_ms or (lambda: int(time() * 1000))
        self._timeout = float(timeout)
        if transport is None:
            session = requests.Session()
            # Não herdar proxies, netrc ou configuração de autenticação genérica do ambiente.
            session.trust_env = False
            self._transport = session
            self._owns_transport = True
        else:
            self._transport = transport
            self._owns_transport = False

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "BinanceSpotReadOnlyAuditClient":
        """Usa exclusivamente AUDIT_BINANCE_READONLY_*; nunca recorre às chaves LIVE."""
        api_key = os.environ.get("AUDIT_BINANCE_READONLY_API_KEY", "").strip()
        api_secret = os.environ.get("AUDIT_BINANCE_READONLY_SECRET", "").strip()
        if not api_key or not api_secret:
            raise BinanceAuditConfigurationError(
                "Defina AUDIT_BINANCE_READONLY_API_KEY e AUDIT_BINANCE_READONLY_SECRET."
            )
        if api_key == os.environ.get("BINANCE_API_KEY", "") or api_secret == os.environ.get("BINANCE_API_SECRET", ""):
            raise BinanceAuditConfigurationError(
                "A credencial de auditoria não pode ser a credencial genérica de negociação."
            )
        return cls(api_key, api_secret, **kwargs)

    def close(self) -> None:
        """Fecha a sessão de transporte, se criada pelo próprio cliente."""
        if self._owns_transport:
            self._transport.close()

    def fetch_account(self) -> dict[str, Any]:
        result = self._get("account", {})
        if not isinstance(result, dict) or not isinstance(result.get("balances"), list):
            raise BinanceAuditRequestError("Resposta de saldo Binance inválida.")
        return result

    def fetch_bnb_usdt_open_orders(self) -> list[dict[str, Any]]:
        result = self._get("open_orders", {"symbol": _ALLOWED_SYMBOL})
        if not isinstance(result, list):
            raise BinanceAuditRequestError("Resposta de ordens abertas Binance inválida.")
        return result

    def fetch_bnb_usdt_orders(self, start_time_ms: int, end_time_ms: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "all_orders", start_time_ms=start_time_ms, end_time_ms=end_time_ms
        )

    def fetch_bnb_usdt_trades(self, start_time_ms: int, end_time_ms: int | None = None) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "my_trades", start_time_ms=start_time_ms, end_time_ms=end_time_ms
        )

    def _fetch_paginated(
        self, route: str, *, start_time_ms: int, end_time_ms: int | None
    ) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        cursor: int | None = None
        for page in range(_MAX_PAGES):
            params: dict[str, Any] = {"symbol": _ALLOWED_SYMBOL, "limit": _MAX_PAGE_SIZE}
            if cursor is None:
                params["startTime"] = int(start_time_ms)
            elif route == "all_orders":
                params["orderId"] = cursor
            else:
                params["fromId"] = cursor
            if end_time_ms is not None:
                params["endTime"] = int(end_time_ms)
            rows = self._get(route, params)
            if not isinstance(rows, list):
                raise BinanceAuditRequestError("Resposta de histórico Binance inválida.")
            all_rows.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < _MAX_PAGE_SIZE:
                return all_rows
            last_row = rows[-1]
            last_id = last_row.get("orderId") if route == "all_orders" else last_row.get("id")
            if last_id is None:
                raise BinanceAuditRequestError("Paginação Binance incompleta; identificador ausente.")
            next_cursor = int(last_id) + 1
            if cursor is not None and next_cursor <= cursor:
                raise BinanceAuditRequestError("Paginação Binance sem avanço; auditoria incompleta.")
            cursor = next_cursor
        raise BinanceAuditRequestError("Limite seguro de páginas atingido; auditoria incompleta.")

    def _get(self, route_name: str, params: Mapping[str, Any]) -> Any:
        route = _ALLOWED_ROUTES.get(route_name)
        if route is None:
            raise BinanceAuditRequestError("Rota Binance não permitida para auditoria.")
        url = _BASE_URL + route
        payload = [(key, value) for key, value in params.items() if value is not None]
        payload.extend((("timestamp", int(self._clock_ms())), ("recvWindow", 5000)))
        signature = hmac.new(
            self._api_secret,
            urlencode(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload.append(("signature", signature))
        try:
            response = self._transport.get(
                url,
                params=payload,
                headers={"X-MBX-APIKEY": self._api_key},
                timeout=self._timeout,
                allow_redirects=False,
            )
        except Exception as exc:
            # Não propaga a exceção original: requests pode incluir URL assinada/query string.
            raise BinanceAuditRequestError(
                f"Falha de transporte GET na Binance ({type(exc).__name__}); detalhes omitidos."
            ) from None

        parsed_url = urlparse(getattr(response, "url", url) or url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "api.binance.com"
            or parsed_url.port not in (None, 443)
            or parsed_url.path != route
            or getattr(response, "status_code", 0) in (301, 302, 303, 307, 308)
        ):
            raise BinanceAuditRequestError("Destino/redirecionamento inesperado; resposta recusada.")
        status_code = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status_code < 300:
            raise BinanceAuditRequestError(f"Binance respondeu HTTP {status_code}; corpo omitido.")
        try:
            result = response.json()
        except Exception:
            raise BinanceAuditRequestError("Resposta JSON Binance inválida; corpo omitido.") from None
        if isinstance(result, dict) and "code" in result and int(result.get("code", 0)) < 0:
            raise BinanceAuditRequestError(
                f"Binance retornou código API {int(result['code'])}; mensagem omitida."
            )
        return result
