import ccxt

ex = ccxt.binance()
markets = ex.load_markets()
btc_usdt = ex.markets['BTC/USDT']
print("BTC/USDT Market Limits:")
limits = btc_usdt.get('limits', {})
print(f"  Amount min: {limits.get('amount', {}).get('min')}")
print(f"  Amount max: {limits.get('amount', {}).get('max')}")
print(f"  Cost (USDT value) min: {limits.get('cost', {}).get('min')}")
print(f"  Cost (USDT value) max: {limits.get('cost', {}).get('max')}")
print(f"\nOur quantity: 0.00008 BTC")
print(f"Our cost (at ~64k USDT): ~5.12 USDT")
