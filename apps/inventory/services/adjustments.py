"""Protected stock adjustments — owner-only, audited, idempotent, never negative."""

from __future__ import annotations

from django.db import transaction

from apps.core.exceptions import APIError
from apps.core.services.audit import record_audit
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.stock import lock_balances, write_movement

_DIRECTIONS = {"INCREASE", "DECREASE"}
_MIN_REASON = 10


@transaction.atomic
def adjust_stock(
    *,
    branch,
    variant,
    direction: str,
    quantity: int,
    reason: str,
    actor,
    client_adjustment_id,
    request=None,
) -> InventoryBalance:
    if direction not in _DIRECTIONS:
        raise APIError(
            "Direction must be INCREASE or DECREASE.",
            code="invalid_direction",
            field_errors={"direction": ["Choose INCREASE or DECREASE."]},
        )
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        raise APIError(
            "Quantity must be a positive whole number.",
            code="invalid_quantity",
            field_errors={"quantity": ["Must be greater than zero."]},
        )
    reason = (reason or "").strip()
    if len(reason) < _MIN_REASON:
        raise APIError(
            "A detailed reason is required.",
            code="reason_too_short",
            field_errors={"reason": [f"At least {_MIN_REASON} characters."]},
        )

    from apps.accounts.services.offline_session import assert_no_active_offline_session

    assert_no_active_offline_session(branch)

    def _already_applied():
        return StockMovement.objects.filter(
            branch=branch,
            reference_type="adjustment",
            reference_id=client_adjustment_id,
        ).exists()

    # Fast path for a sequential retry.
    if _already_applied():
        return InventoryBalance.objects.get(branch=branch, variant=variant)

    # Serialise on the balance row, then re-check: a concurrent request that
    # won the race has now committed its movement.
    balance = lock_balances(branch, [variant.id])[variant.id]
    if _already_applied():
        return balance
    old_quantity = balance.quantity
    delta = quantity if direction == "INCREASE" else -quantity

    write_movement(
        balance=balance,
        delta=delta,
        movement_type=MovementType.ADJUSTMENT,
        created_by=actor,
        reference_type="adjustment",
        reference_id=client_adjustment_id,
        reason=reason,
    )
    balance.refresh_from_db()

    record_audit(
        action="stock.adjust",
        target=variant,
        actor=actor,
        branch=branch,
        request=request,
        before={"quantity": old_quantity},
        after={
            "quantity": balance.quantity,
            "change": delta,
            "direction": direction,
            "reason": reason,
        },
    )
    return balance
