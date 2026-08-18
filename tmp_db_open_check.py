from database.connection import get_session
from sqlalchemy import text
import json

with get_session() as session:
    open_rows = session.execute(text("SELECT symbol, timeframe, strategy_name, side, status, is_paper, quantity, entry_price, entry_time FROM trades WHERE status = 'OPEN' ORDER BY entry_time")).fetchall()
    print('OPEN_ROWS', len(open_rows))
    print(json.dumps([dict(r._mapping) for r in open_rows], default=str))

    recent_history = session.execute(text("SELECT execution_id, strategy, symbol, timeframe, entry_time, exit_time, pnl FROM trade_history WHERE exit_time IS NULL OR exit_time >= DATE_SUB(NOW(), INTERVAL 7 DAY) ORDER BY exit_time DESC LIMIT 20")).fetchall()
    print('HISTORY_RECENT', len(recent_history))
    print(json.dumps([dict(r._mapping) for r in recent_history], default=str))
