"""Presentation-only cost breakdown from persisted fills; never changes accounting."""
from decimal import Decimal, InvalidOperation


def _number(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def transaction_costs(row):
    result = dict(row)
    quantity, price = _number(row.get('filled_quantity')), _number(row.get('price'))
    fee = _number(row.get('fee_usdt'))
    gross = None
    if str(row.get('symbol', '')).endswith('/USDT') and quantity is not None and quantity >= 0:
        if quantity == 0:
            gross = Decimal(0)
        elif price is not None and price > 0:
            gross = quantity * price
    side = str(row.get('side', '')).upper()
    equivalent = None
    if gross is not None and fee is not None and side in {'BUY', 'SELL'}:
        equivalent = gross + fee if side == 'BUY' else gross - fee
    result.update(
        executed_value_usdt=float(gross) if gross is not None else None,
        buy_cost_equivalent_usdt=float(equivalent) if equivalent is not None and side == 'BUY' else None,
        sell_proceeds_equivalent_usdt=float(equivalent) if equivalent is not None and side == 'SELL' else None,
        conversion_charge_usdt=None,
    )
    return result
