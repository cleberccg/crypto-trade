"""
Position sizing calculations.

Design decision: Separating position sizing from the risk manager keeps each
class focused on a single responsibility.  All sizing methods are pure
functions with no side effects.
"""
from __future__ import annotations

from utils.logger import get_logger
from utils.validators import validate_positive_float, validate_percentage

logger = get_logger(__name__)


class PositionSizer:
    """
    Calculates the position size for a trade.

    Supports two sizing methods:
    1. **Fixed fractional**: stake a fixed percentage of total portfolio.
    2. **Risk-based**: determine size so that the stop-loss hit equals a fixed
       percentage of total portfolio (Kelly/fixed-risk approach).
    """

    def fixed_fractional(
        self,
        portfolio_value: float,
        stake_pct: float,
        price: float,
    ) -> float:
        """
        Size a position as a fixed fraction of the portfolio.

        Args:
            portfolio_value: Total portfolio value in quote currency.
            stake_pct: Fraction to stake per trade (e.g. 0.02 for 2%).
            price: Current asset price.

        Returns:
            Quantity to buy in base currency.
        """
        portfolio_value = validate_positive_float(portfolio_value, "portfolio_value")
        stake_pct = validate_percentage(stake_pct, "stake_pct")
        price = validate_positive_float(price, "price")

        stake_amount = portfolio_value * stake_pct
        quantity = stake_amount / price

        logger.debug(
            "fixed_fractional - portfolio=%.2f stake_pct=%.4f price=%.4f "
            "stake=%.2f qty=%.6f",
            portfolio_value,
            stake_pct,
            price,
            stake_amount,
            quantity,
        )
        return quantity

    def risk_based(
        self,
        portfolio_value: float,
        risk_pct: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> float:
        """
        Size a position so a stop-loss hit loses exactly *risk_pct* of the
        portfolio.

        Formula: qty = (portfolio * risk_pct) / (entry - stop_loss)

        Args:
            portfolio_value: Total portfolio value in quote currency.
            risk_pct: Maximum loss fraction per trade (e.g. 0.01 for 1%).
            entry_price: Planned entry price.
            stop_loss_price: Planned stop-loss price.

        Returns:
            Quantity to buy in base currency.

        Raises:
            ValueError: If stop_loss_price >= entry_price (no room for loss).
        """
        portfolio_value = validate_positive_float(portfolio_value, "portfolio_value")
        risk_pct = validate_percentage(risk_pct, "risk_pct")
        entry_price = validate_positive_float(entry_price, "entry_price")
        stop_loss_price = validate_positive_float(stop_loss_price, "stop_loss_price")

        risk_per_unit = entry_price - stop_loss_price
        if risk_per_unit <= 0:
            raise ValueError(
                f"stop_loss_price ({stop_loss_price}) must be less than "
                f"entry_price ({entry_price})."
            )

        max_loss = portfolio_value * risk_pct
        quantity = max_loss / risk_per_unit

        logger.info(
            "PositionSizer.risk_based details - portfolio=%.2f risk_pct=%.4f "
            "entry=%.6f stop=%.6f risk_per_unit=%.6f max_loss=%.2f qty=%.8f",
            portfolio_value,
            risk_pct,
            entry_price,
            stop_loss_price,
            risk_per_unit,
            max_loss,
            quantity,
        )

        logger.debug(
            "risk_based - portfolio=%.2f risk_pct=%.4f entry=%.4f sl=%.4f "
            "risk_per_unit=%.4f max_loss=%.2f qty=%.6f",
            portfolio_value,
            risk_pct,
            entry_price,
            stop_loss_price,
            risk_per_unit,
            max_loss,
            quantity,
        )
        return quantity
