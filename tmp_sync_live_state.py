import json
from pathlib import Path
from database.connection import get_session
from database.models import Trade

STATE_PATH = Path('optimization/results/live_position_BTC_USDT_15m.json')

with get_session() as session:
    trade = (
        session.query(Trade)
        .filter(
            Trade.symbol == 'BTC/USDT',
            Trade.strategy_name == 'ClassicDonchianBreakout',
            Trade.status == 'open',
        )
        .order_by(Trade.id.desc())
        .first()
    )

    if not trade:
        raise SystemExit('No open ClassicDonchianBreakout trade found for BTC/USDT')

    payload = {
        'trade_id': int(trade.id),
        'symbol': str(trade.symbol),
        'quantity': float(trade.quantity),
        'entry_price': float(trade.entry_price),
        'stop_loss': float(trade.stop_loss or 0.0),
        'take_profit': float(trade.take_profit or 0.0),
        'entry_ts': trade.entry_time.isoformat() if trade.entry_time else '',
        'exchange_order_id': '',
    }

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
print(f'State synced to trade_id={payload["trade_id"]}: {STATE_PATH}')
