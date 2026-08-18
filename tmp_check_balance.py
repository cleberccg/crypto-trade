from exchange.binance_client import BinanceClient

ex = BinanceClient()
ex.connect()
try:
    orders = ex.fetch_open_orders('BTC/USDT')
    print(f'Open orders: {len(orders)}')
    for o in orders:
        print(f'  {o.get("side").upper()} qty={o.get("amount"):.8f} price={o.get("price"):.4f}')
    
    balance = ex.fetch_balance()
    btc_balance = balance.get('BTC', {})
    usdt_balance = balance.get('USDT', {})
    print(f'BTC: free={btc_balance.get("free", 0):.8f} used={btc_balance.get("used", 0):.8f}')
    print(f'USDT: free={usdt_balance.get("free", 0):.2f} used={usdt_balance.get("used", 0):.2f}')
finally:
    ex.disconnect()
