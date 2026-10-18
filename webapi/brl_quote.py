"""Public USDT/BRL quote for display only. No credentials or trading imports."""
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

import requests

_lock = Lock()
_until = 0.0
_cached = None


def get_brl_quote():
    global _until, _cached
    with _lock:
        if _cached is not None and monotonic() < _until:
            return dict(_cached)
        result = {'symbol': 'USDTBRL', 'rate': None, 'at': None, 'source': 'Binance Spot USDT/BRL', 'status': 'unavailable'}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get('https://api.binance.com/api/v3/ticker/price', params={'symbol': 'USDTBRL'}, timeout=5, allow_redirects=False)
                destination = urlparse(response.url)
                if response.status_code != 200 or destination.scheme != 'https' or destination.hostname != 'api.binance.com' or destination.path != '/api/v3/ticker/price':
                    raise ValueError('Unexpected public quote response')
                payload = response.json()
                rate = Decimal(payload['price'])
                if payload.get('symbol') != 'USDTBRL' or not rate.is_finite() or rate <= 0:
                    raise ValueError('Invalid public quote')
                result.update(rate=float(rate), at=datetime.now(timezone.utc).isoformat(), status='available')
        except Exception:
            pass  # No stale rate, response body or transport details are exposed.
        _cached = result
        _until = monotonic() + (60 if result['rate'] is not None else 15)
        return dict(result)
