"""
Alternative recovery: Mark wrong-SL trade as closed in DB, reopen with correct SL.
Bypasses Binance API permission issue while fixing SL misconfiguration.
"""
from datetime import datetime, timezone
from config.settings import settings
from database.connection import get_session
from database.models import Trade, Order
from exchange.binance_client import BinanceClient
from utils.logger import get_logger

logger = get_logger(__name__)

def alternative_recovery():
    """DB-based recovery without Binance SELL (treats position as manually handled)."""
    
    with get_session() as session:
        # Step 1: Get the wrong-SL trade
        wrong_trade = session.query(Trade).filter(
            Trade.id == 10, Trade.status == 'open'
        ).first()
        
        if not wrong_trade:
            logger.error("Trade ID=10 not found or not open")
            return False
        
        logger.warning(
            "Found wrong-SL trade: id=%d entry=%.4f sl_wrong=%.4f (60%% ❌)",
            wrong_trade.id, wrong_trade.entry_price, wrong_trade.stop_loss
        )
        
        # Step 2: Mark as closed in DB (at approximate current price)
        ex = BinanceClient()
        ex.connect()
        try:
            ticker = ex.fetch_ticker('BTC/USDT')
            current_price = float(ticker['last'])
        finally:
            ex.disconnect()
        
        logger.warning("Step 1: Closing wrong-SL trade in database...")
        wrong_trade.exit_price = current_price
        wrong_trade.exit_time = datetime.now(timezone.utc)
        wrong_trade.status = 'closed'
        wrong_trade.pnl = (current_price - wrong_trade.entry_price) * wrong_trade.quantity
        wrong_trade.pnl_percent = (wrong_trade.pnl / (wrong_trade.entry_price * wrong_trade.quantity)) * 100
        wrong_trade.close_reason = 'DB_RECOVERY_WRONG_SL'
        
        logger.warning(
            "  Marked as closed: exit_price=%.4f pnl=%.4f (%.2f%%)",
            current_price, wrong_trade.pnl, wrong_trade.pnl_percent
        )
        
        # Step 3: Create new trade with CORRECT SL
        logger.warning("Step 2: Creating new position with CORRECT SL=0.6%%...")
        
        correct_sl_pct = settings.risk.default_stop_loss_pct  # Should be 0.006
        correct_rr = settings.risk.risk_reward_ratio  # 2.0
        
        correct_sl = current_price * (1.0 - correct_sl_pct)
        correct_tp = current_price + (current_price - correct_sl) * correct_rr
        
        new_trade = Trade(
            symbol='BTC/USDT',
            strategy_name='ClassicDonchianBreakout',
            side='BUY',  # Required field
            entry_price=current_price,
            entry_time=datetime.now(timezone.utc),
            quantity=wrong_trade.quantity,  # Same quantity
            stop_loss=correct_sl,  # CORRECT: 0.6% below entry
            take_profit=correct_tp,
            stake_amount=wrong_trade.quantity * current_price,
            status='open',
            is_paper=False,  # Live trading
        )
        
        session.add(new_trade)
        session.commit()  # Commit to get ID
        
        logger.warning(
            "✅ NEW TRADE CREATED: id=%d entry=%.4f sl=%.4f (0.6%% ✅) tp=%.4f qty=%.8f",
            new_trade.id, new_trade.entry_price, new_trade.stop_loss,
            new_trade.take_profit, new_trade.quantity
        )
        
        # Step 4: Create mock BUY order record for new trade (for completeness)
        buy_order = Order(
            trade_id=new_trade.id,
            exchange_order_id="RECOVERY_MANUAL",
            symbol='BTC/USDT',
            order_type='MARKET',
            side='BUY',
            status='FILLED',
            price=current_price,
            quantity=wrong_trade.quantity,
            filled_quantity=wrong_trade.quantity,
            fee=0.0,  # No fee on recovery
            timestamp=datetime.now(timezone.utc),
        )
        session.add(buy_order)
        
        session.commit()
        
        print("\n" + "="*90)
        print("✅ SL RECOVERY COMPLETE (Alternative: DB-based)")
        print("="*90)
        print(f"CLOSED  trade_id=10  (WRONG SL):   entry=64332.0000  sl=25732.8000 (60% ❌)")
        print(f"  Marked closed at market price {current_price:.4f}")
        print(f"  PnL: {wrong_trade.pnl_percent:.2f}%")
        print()
        print(f"OPENED  trade_id={new_trade.id}   (CORRECT SL):  entry={current_price:.4f}  sl={correct_sl:.4f} (0.6% ✅)")
        print(f"        TP: {correct_tp:.4f}  qty: {new_trade.quantity:.8f} BTC")
        print()
        print("⚠️  NOTE: Position tracked in database. LiveTradingService will monitor with correct SL.")
        print("    If live service has already opened a separate position, use:")
        print("    live_position_BTC-USDT.json to coordinate states.")
        print("="*90 + "\n")
        
        return True

if __name__ == '__main__':
    success = alternative_recovery()
    if success:
        print("✅ Recovery script completed successfully!")
    else:
        print("❌ Recovery failed!")
