"""Decimal money helpers. Never use float for naira."""

from decimal import ROUND_HALF_UP, Decimal

MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")


def to_money(value) -> Decimal:
    """Coerce to Decimal and round to kobo with banker-free HALF_UP."""

    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def weighted_average_cost(
    *,
    old_quantity: int,
    old_average: Decimal,
    received_quantity: int,
    received_unit_cost: Decimal,
) -> Decimal:
    """Weighted average of old and received stock, rounded to kobo.

    ``(old_qty*old_avg + recv_qty*recv_cost) / (old_qty + recv_qty)``.
    """

    total_quantity = old_quantity + received_quantity
    if total_quantity <= 0:
        return ZERO
    numerator = Decimal(old_quantity) * Decimal(old_average) + Decimal(
        received_quantity
    ) * Decimal(received_unit_cost)
    return to_money(numerator / Decimal(total_quantity))
