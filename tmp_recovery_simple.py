"""Simplified recovery: Direct market SELL via exchange, then update DB and reopen."""
from datetime import datetime, timezone
from config.settings import settings
from database.connection import get_session
from database.models import Trade, Order
from exchange.binance_client import BinanceClient
from utils.logger import get_logger

logger = get_logger(__name__)

def simple_recovery():
    """Direct approach: SELL -> close DB -> reopen with correct SL."""
    
    ex = BinanceClient()
    ex.connect()
    
    try:
        with get_session() as session:
            # Get the wrong-SL trade
            wrong_trade = session.query(Trade).filter(
                Trade.id == 10, Trade.status == 'open'
            ).first()
            
            if not wrong_trade:
                logger.error("Trade ID=10 not found or not open")
                return
            
            logger.info(
                "Found trade: id=%d qty=%.8f entry=%.4f sl_wrong=%.4f",
                wrong_trade.id, wrong_trade.quantity, wrong_trade.entry_price,
                wrong_trade.stop_loss
            )
            
            # Step 1: Market SELL
            logger.warning("Step 1: Executing market SELL...")
            ticker = ex.fetch_ticker('BTC/USDT')
            current_price = float(ticker['last'])
            
            sell_order = ex.create_market_order('BTC/USDT', 'sell', wrong_trade.quantity)
            fill_price = float(sell_order.get('average', sell_order.get('price', current_price)))
            fill_qty = float(sell_order.get('filled', wrong_trade.quantity))
            
            logger.warning(
                "✅ Market SELL executed: qty=%.8f fill_price=%.4f exchange_id=%s",
                fill_qty, fill_price, sell_order['id']
            )
            
            # Step 2: Close the trade in DB
            logger.info("Step 2: Closing trade in database...")
            wrong_trade.exit_price = fill_price
            wrong_trade.exit_time = datetime.now(timezone.utc)
            wrong_trade.status = 'closed'
            wrong_trade.pnl = (fill_price - wrong_trade.entry_price) * wrong_trade.quantity
            wrong_trade.pnl_percent = (wrong_trade.pnl / (wrong_trade.entry_price * wrong_trade.quantity)) * 100
            wrong_trade.close_reason = 'SL_RECOVERY_FIX'
            
            # Record the SELL order
            sell_order_rec = Order(
                trade_id=wrong_trade.id,
                exchange_order_id=str(sell_order['id']),
                symbol='BTC/USDT',
                order_type='MARKET',
                side='SELL',
                status='FILLED',
                price=fill_price,
                quantity=wrong_trade.quantity,
                filled_quantity=fill_qty,
                fee=float(sell_order.get('fee', {}).get('cost', 0.0)),
                timestamp=datetime.now(timezone.utc),
            )
            session.add(sell_order_rec)
            session.commit()
            
            logger.info(
                "Trade closed: pnl=%.4f (%.2f%%) close_price=%.4f",
                wrong_trade.pnl, wrong_trade.pnl_percent, fill_price
            )
            
            # Step 3: Check new balance
            balance = ex.fetch_balance()
            free_usdt = float(balance.get('USDT', {}).get('free', 0))
            logger.info("Free USDT after SELL: %.2f", free_usdt)
            
            if free_usdt < 5.0:
                logger.warning("⚠️ Balance too low to reopen: %.2f USDT", free_usdt)
                return
            
            # Step 4: Reopen with CORRECT SL
            logger.warning("Step 3: Reopening with CORRECT SL=0.6%%...")
            
            correct_sl_pct = settings.risk.default_stop_loss_pct  # 0.006
            correct_rr = settings.risk.risk_reward_ratio
            
            correct_sl = current_price * (1.0 - correct_sl_pct)
            correct_tp = current_price + (current_price - correct_sl) * correct_rr
            
            # Calculate position size (simple: use same stake)
            qty_new = wrong_trade.quantity
            
            logger.info(
                "New entry: price=%.4f sl=%.4f (%.4f%%) tp=%.4f qty=%.8f",
                current_price, correct_sl, correct_sl_pct * 100, correct_tp, qty_new
            )
            
            # Execute BUY
            buy_order = ex.create_market_order('BTC/USDT', 'buy', qty_new)
            fill_price_buy = float(buy_order.get('average', buy_order.get('price', current_price)))
            fill_qty_buy = float(buy_order.get('filled', qty_new))
            
            logger.warning(
                "✅ Market BUY executed: qty=%.8f fill_price=%.4f exchange_id=%s",
                fill_qty_buy, fill_price_buy, buy_order['id']
            )
            
            # Create new trade
            new_trade = Trade(
                symbol='BTC/USDT',
                strategy='ClassicDonchianBreakout',
                entry_price=fill_price_buy,
                entry_time=datetime.now(timezone.utc),
                quantity=fill_qty_buy,
                stop_loss=correct_sl,
                take_profit=correct_tp,
                stake_amount=fill_qty_buy * fill_price_buy,
                status='open',
            )
            session.add(new_trade)
            
            # Record BUY order
            buy_order_rec = Order(
                trade_id=new_trade.id,
                exchange_order_id=str(buy_order['id']),
                symbol='BTC/USDT',
                order_type='MARKET',
                side='BUY',
                status='FILLED',
                price=fill_price_buy,
                quantity=qty_new,
                filled_quantity=fill_qty_buy,
                fee=float(buy_order.get('fee', {}).get('cost', 0.0)),
                timestamp=datetime.now(timezone.utc),
            )
            session.add(buy_order_rec)
            session.commit()
            
            logger.warning(
                "✅ NEW TRADE CREATED: id=%d entry=%.4f sl=%.4f (CORRECT 0.6%%) tp=%.4f qty=%.8f",
                new_trade.id, new_trade.entry_price, new_trade.stop_loss,
                new_trade.take_profit, new_trade.quantity
            )
            
            print("\n" + "="*80)
            print("✅ SL RECOVERY COMPLETE!")
            print("="*80)
            print(f"CLOSED trade_id=10  (WRONG SL):    entry=64332.00  sl=25732.80 (60% ❌)")
            print(f"  Closed at {fill_price:.2f}, PnL: {wrong_trade.pnl_percent:.2f}%")
            print()
            print(f"OPENED trade_id={new_trade.id}   (CORRECT SL):  entry={fill_price_buy:.2f}  sl={correct_sl:.2f} (0.6% ✅)")
            print(f"  Ready for monitoring with correct Stop Loss!")
            print("="*80 + "\n")
    
    finally:
        ex.disconnect()

if __name__ == '__main__':
    simple_recovery()
