from database.connection import get_session
from database.models import Trade

with get_session() as session:
    trades = session.query(Trade).filter(
        Trade.symbol == 'BTC/USDT'
    ).order_by(Trade.id.desc()).limit(5).all()
    
    print("Recent BTC/USDT Trades:")
    print("="*100)
    for t in trades:
        status_icon = "✅ OPEN" if t.status == 'open' else "⏹️ CLOSED"
        sl_pct = ((t.entry_price - t.stop_loss) / t.entry_price * 100) if t.stop_loss else 0
        
        print(f"ID={t.id}  {status_icon}  Entry={t.entry_price:.4f}  SL={t.stop_loss:.4f} ({sl_pct:.1f}%)  TP={t.take_profit:.4f}")
        if t.status == 'closed' and t.exit_price:
            print(f"       Exit={t.exit_price:.4f}  PnL={t.pnl:.6f} ({t.pnl_pct:.2f}%)" if t.pnl_pct else f"       Exit={t.exit_price:.4f}  PnL={t.pnl:.6f}")
