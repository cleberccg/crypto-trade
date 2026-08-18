from database.connection import get_session
from database.models import Trade
from exchange.binance_client import BinanceClient

TRADE_ID = 11

ex = BinanceClient()
ex.connect()
try:
    balance = ex.fetch_balance()
    free_btc = float(balance.get('BTC', {}).get('free', 0.0))
finally:
    ex.disconnect()

if free_btc <= 0:
    raise SystemExit('No free BTC balance found')

with get_session() as session:
    trade = session.query(Trade).filter(Trade.id == TRADE_ID, Trade.status == 'open').first()
    if not trade:
        raise SystemExit(f'Open trade {TRADE_ID} not found')

    old_qty = float(trade.quantity)
    trade.quantity = free_btc
    trade.stake_amount = float(trade.entry_price) * free_btc
    session.commit()

print(f'Adjusted trade_id={TRADE_ID} quantity: {old_qty:.8f} -> {free_btc:.8f}')
