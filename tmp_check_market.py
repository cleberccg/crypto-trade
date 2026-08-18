from exchange.binance_client import BinanceClient

ex = BinanceClient()
ex.connect()

try:
    market = ex.fetch_market('BTC/USDT')
    print("BTC/USDT Market Info:")
    print(f"  Limits: {market.get('limits', {})}")
    print(f"  Min amount: {market.get('limits', {}).get('amount', {}).get('min')}")
    print(f"  Max amount: {market.get('limits', {}).get('amount', {}).get('max')}")
    print(f"  Min cost: {market.get('limits', {}).get('cost', {}).get('min')}")
    print(f"  Max cost: {market.get('limits', {}).get('cost', {}).get('max')}")
finally:
    ex.disconnect()
