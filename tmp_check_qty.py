import ccxt

ex = ccxt.binance()
ticker = ex.fetch_ticker('BTC/USDT')
current_price = ticker['last']

qty = 0.00008
current_cost = qty * current_price
min_cost = 5.0

print(f"Current BTC price: {current_price:.2f} USDT")
print(f"Order value: {qty:.8f} BTC × {current_price:.2f} = {current_cost:.4f} USDT")
print(f"Minimum required: {min_cost:.2f} USDT")
print(f"Status: {'✅ OK' if current_cost >= min_cost else '❌ BELOW MINIMUM'}")

if current_cost < min_cost:
    print(f"\n⚠️ Quantity too small! Need at least {min_cost / current_price:.8f} BTC")
