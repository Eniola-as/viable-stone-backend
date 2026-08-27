"""Low-level stock primitives. Every balance change writes exactly one movement.

Callers MUST already be inside ``transaction.atomic()``. Balances are locked with
``select_for_update`` in ascending variant-id order so concurrent restocks and
sales serialise deterministically and never deadlock.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import APIError, Conflict
from apps.core.money import to_money
from apps.core.services.audit import record_audit
from apps.inventory.models import (
    InventoryBalance,
    MovementType,
    StockMovement,
)
from apps.inventory.signals import stock_balance_changed

_BALANCE_FIELDS = ["quantity", "average_unit_cost", "last_restocked_at", "updated_at"]


def lock_balances(branch, variant_ids) -> dict:
    """Get-or-create and row-lock one balance per variant, in sorted id order."""

    locked: dict = {}
    for variant_id in sorted(set(variant_ids), key=str):
        balance, _ = InventoryBalance.objects.select_for_update().get_or_create(
            branch=branch,
            variant_id=variant_id,
            defaults={"quantity": 0, "average_unit_cost": Decimal("0.00")},
        )
        locked[variant_id] = balance
    return locked


def write_movement(
    *,
    balance: InventoryBalance,
    delta: int,
    movement_type: str,
    created_by=None,
    unit_cost_snapshot: Decimal | None = None,
    reference_type: str = "",
    reference_id=None,
    reason: str = "",
    allow_negative: bool = False,
) -> StockMovement | None:
    """Apply ``delta`` to a locked balance and append one movement row."""

    if delta == 0:
        return None
    previous_quantity = balance.quantity
    new_quantity = previous_quantity + delta
    if new_quantity < 0 and not allow_negative:
        raise APIError(
            f"Only {balance.quantity} unit(s) of {balance.variant.sku} are available.",
            code="stock_not_available",
            status_code=409,
        )
    balance.quantity = new_quantity
    balance.save(update_fields=_BALANCE_FIELDS)
    movement = StockMovement.objects.create(
        branch=balance.branch,
        variant=balance.variant,
        movement_type=movement_type,
        quantity_delta=delta,
        unit_cost_snapshot=(
            to_money(unit_cost_snapshot) if unit_cost_snapshot is not None else None
        ),
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        created_by=created_by,
    )
    # Central hook: every workflow's balance change is announced from this one
    # place, while the row is still locked, so listeners (low/out-of-stock
    # notifications) stay consistent across sales, returns, restocks, counts
    # and adjustments.
    stock_balance_changed.send(
        sender=InventoryBalance,
        branch=balance.branch,
        variant=balance.variant,
        previous_quantity=previous_quantity,
        new_quantity=new_quantity,
        movement=movement,
        actor=created_by,
    )
    return movement


@transaction.atomic
def open_stock(
    *, branch, variant, quantity: int, unit_cost, created_by, request=None
) -> InventoryBalance:
    """Establish opening stock. Allowed only before any other movement exists."""

    if quantity <= 0:
        raise APIError(
            "Opening quantity must be a positive whole number.",
            code="invalid_quantity",
            field_errors={"quantity": ["Must be greater than zero."]},
        )
    if Decimal(unit_cost) <= 0:
        raise APIError(
            "Opening unit cost must be greater than zero.",
            code="invalid_cost",
            field_errors={"unit_cost": ["Must be greater than zero."]},
        )
    if StockMovement.objects.filter(branch=branch, variant=variant).exists():
        raise Conflict(
            "Opening stock can only be set before any other stock movement.",
            code="opening_stock_locked",
        )

    balance, _ = InventoryBalance.objects.select_for_update().get_or_create(
        branch=branch,
        variant=variant,
        defaults={"quantity": 0, "average_unit_cost": Decimal("0.00")},
    )
    if balance.quantity != 0:
        raise Conflict(
            "This item already holds stock; opening stock cannot be set again.",
            code="opening_stock_locked",
        )

    now = timezone.now()
    balance.quantity = quantity
    balance.average_unit_cost = to_money(unit_cost)
    balance.last_restocked_at = now
    balance.save(update_fields=_BALANCE_FIELDS)
    StockMovement.objects.create(
        branch=branch,
        variant=variant,
        movement_type=MovementType.OPENING,
        quantity_delta=quantity,
        unit_cost_snapshot=to_money(unit_cost),
        reference_type="opening",
        reason="Opening stock",
        created_by=created_by,
    )
    record_audit(
        action="stock.opening",
        target=variant,
        actor=created_by,
        branch=branch,
        request=request,
        after={"quantity": quantity, "unit_cost": str(to_money(unit_cost))},
    )
    return balance
