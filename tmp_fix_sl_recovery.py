"""
Recovery script: Close trade with wrong SL and reopen with correct SL=0.6%
"""
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from database.connection import get_session
from database.models import Trade
from exchange.binance_client import BinanceClient
from execution.order_executor import OrderExecutor
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.portfolio_value_provider import BinancePortfolioValueProvider
from utils.logger import get_logger

logger = get_logger(__name__)


def fix_sl_recovery():
    """Close wrong-SL trade and reopen with correct SL."""
    
    exchange = BinanceClient()
    exchange.connect()
    
    try:
        with get_session() as session:
            # Find the wrong-SL trade
            wrong_trade = session.query(Trade).filter(
                Trade.symbol == 'BTC/USDT',
                Trade.status == 'open',
                Trade.id == 10
            ).first()
            
            if not wrong_trade:
                logger.error("Trade ID=10 not found or not open")
                return
            
            logger.info("Found wrong-SL trade: id=%d entry=%.4f sl_wrong=%.4f tp=%.4f qty=%.8f",
                wrong_trade.id, wrong_trade.entry_price, wrong_trade.stop_loss,
                wrong_trade.take_profit, wrong_trade.quantity
            )
            
            # Fetch current market price (use once for both operations)
            try:
                ticker = exchange.fetch_ticker('BTC/USDT')
                current_price = float(ticker['last'])
                logger.info("Current market price: %.4f", current_price)
            except Exception as exc:
                logger.error("Failed to fetch current market price: %s", exc)
                return
            
            logger.info(
                "Found wrong-SL trade: id=%d entry=%.4f sl_wrong=%.4f tp=%.4f qty=%.8f",
                wrong_trade.id, wrong_trade.entry_price, wrong_trade.stop_loss,
                wrong_trade.take_profit, wrong_trade.quantity
            )
            
            # Step 1: Close the position via market SELL
            logger.warning("Step 1: Closing wrong-SL position via market SELL...")
            
            order_executor = OrderExecutor(exchange)
            
            try:
                order = order_executor.execute_market_sell(
                    trade=wrong_trade,
                    symbol='BTC/USDT',
                    quantity=wrong_trade.quantity,
                    price=current_price,
                )
                logger.info(
                    "Market SELL executed: qty=%.8f fill_price=%.4f exchange_id=%s",
                    order.filled_quantity, order.price, order.exchange_order_id
                )
                close_price = order.price
            except Exception as exc:
                logger.error("Failed to execute market SELL: %s", exc)
                return
            
            # Step 2: Update trade status to closed
            logger.info("Step 2: Updating trade status to CLOSED...")
            wrong_trade.exit_price = close_price
            wrong_trade.exit_time = datetime.now(timezone.utc)
            wrong_trade.status = 'closed'
            wrong_trade.pnl = (close_price - wrong_trade.entry_price) * wrong_trade.quantity
            wrong_trade.pnl_percent = (wrong_trade.pnl / (wrong_trade.entry_price * wrong_trade.quantity)) * 100
            wrong_trade.close_reason = 'SL_RECOVERY_FIX'
            session.commit()
            logger.info(
                "Trade closed: pnl=%.4f (%.2f%%) close_price=%.4f",
                wrong_trade.pnl, wrong_trade.pnl_percent, close_price
            )
            
            # Step 3: Get portfolio balance and reopen
            logger.info("Step 3: Calculating portfolio and reopening with correct SL...")
            portfolio_provider = BinancePortfolioValueProvider(exchange)
            free_balance = portfolio_provider.get_free_balance('USDT')
            logger.info("Free balance after close: %.2f USDT", free_balance)
            
            if free_balance < 5.0:
                logger.warning("Free balance too low to reopen: %.2f USDT", free_balance)
                return
            
            # Calculate SL/TP with CORRECT percentage
            correct_sl_pct = settings.risk.default_stop_loss_pct  # should be 0.006
            correct_rr = settings.risk.risk_reward_ratio
            
            correct_sl = current_price * (1.0 - correct_sl_pct)
            correct_tp = current_price + (current_price - correct_sl) * correct_rr
            
            logger.info(
                "New entry params: price=%.4f sl=%.4f (%.4f%%) tp=%.4f rr=%.2f",
                current_price, correct_sl, correct_sl_pct * 100, correct_tp, correct_rr
            )
            
            # Use risk manager to size the position
            sizer = PositionSizer()
            risk_manager = RiskManager(settings.risk)
            
            risk_result = risk_manager.evaluate_trade(
                entry_price=current_price,
                stop_loss=correct_sl,
                take_profit=correct_tp,
                portfolio_balance=free_balance,
                strategy_score=1.0,  # Full confidence
            )
            
            if not risk_result['approved']:
                logger.error("Risk validation failed: %s", risk_result.get('reason'))
                return
            
            qty_calculated = risk_result['quantity']
            logger.info(
                "Risk sizing approved: qty=%.8f stake=%.2f USDT",
                qty_calculated, risk_result['stake']
            )
            
            # Execute market BUY
            logger.info("Step 4: Executing market BUY with correct SL...")
            
            # First, create the trade object in DB
            new_trade = Trade(
                symbol='BTC/USDT',
                strategy='ClassicDonchianBreakout',
                entry_price=current_price,
                entry_time=datetime.now(timezone.utc),
                quantity=qty_calculated,
                stop_loss=correct_sl,
                take_profit=correct_tp,
                stake_amount=qty_calculated * current_price,
                status='open',
                close_reason=None,
            )
            session.add(new_trade)
            session.commit()
            logger.info("Trade object created in DB: id=%d", new_trade.id)
            
            try:
                order = order_executor.execute_market_buy(
                    trade=new_trade,
                    symbol='BTC/USDT',
                    quantity=qty_calculated,
                    price=current_price,
                )
                logger.warning(
                    "Market BUY executed: qty=%.8f fill_price=%.4f exchange_id=%s",
                    order.filled_quantity, order.price, order.exchange_order_id
                )
            except Exception as exc:
                logger.error("Failed to execute market BUY: %s", exc)
                # Rollback new trade if order failed
                session.query(Trade).filter(Trade.id == new_trade.id).update({'status': 'cancelled'})
                session.commit()
                return
            
            logger.warning(
                "✅ NEW TRADE OPENED: id=%d entry=%.4f sl=%.4f (CORRECT 0.6%%) "
                "tp=%.4f qty=%.8f",
                new_trade.id, new_trade.entry_price, new_trade.stop_loss,
                new_trade.take_profit, new_trade.quantity
            )
            
            logger.info("✅ SL RECOVERY COMPLETE!")
            print("\n" + "="*70)
            print("✅ SL RECOVERY COMPLETED SUCCESSFULLY")
            print("="*70)
            print(f"OLD trade (WRONG SL):  id=10  entry=64332.00  sl=25732.80 (60% ❌)")
            print(f"NEW trade (CORRECT):   id={new_trade.id}  entry={new_trade.entry_price:.2f}  sl={new_trade.stop_loss:.2f} (0.6% ✅)")
            print(f"PnL from recovery:     {wrong_trade.pnl:.4f} USDT ({wrong_trade.pnl_percent:.2f}%)")
            print("="*70 + "\n")
    
    except Exception:
        logger.exception("Recovery failed")
        raise
    finally:
        exchange.disconnect()


if __name__ == '__main__':
    fix_sl_recovery()
