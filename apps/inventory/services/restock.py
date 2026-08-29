"""Restock confirmation with weighted-average costing. Confirms exactly once."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money, weighted_average_cost
from apps.core.services.audit import record_audit
from apps.inventory.models import MovementType, Restock, RestockStatus
from apps.inventory.services.stock import lock_balances, write_movement


def restock_purchase_total(restock: Restock) -> Decimal:
    """The purchase total of the delivery: the exact Decimal sum of every
    item's ``quantity * unit_cost``, rounded to kobo.

    This is the meaning of ``Restock.total_cost`` in every state — a DRAFT
    header carries the same total as it will after confirmation. Confirmation
    applies stock and weighted-average cost; it does not change this arithmetic.
    """

    total = ZERO
    for quantity, unit_cost in restock.items.values_list("quantity", "unit_cost"):
        total += Decimal(quantity) * unit_cost
    return to_money(total)


@transaction.atomic
def confirm_restock(*, restock: Restock, confirmed_by, request=None) -> Restock:
    """Apply a draft restock to inventory. All-or-nothing; never twice."""

    locked = (
        Restock.objects.select_for_update().select_related("branch").get(pk=restock.pk)
    )
    if locked.status == RestockStatus.CONFIRMED:
        raise Conflict(
            "This restock has already been confirmed.",
            code="restock_already_confirmed",
        )

    items = list(locked.items.select_related("variant").order_by("variant_id"))
    if not items:
        raise APIError(
            "Add at least one item before confirming this restock.",
            code="restock_empty",
        )
    if any(i.variant.product.branch_id != locked.branch_id for i in items):
        raise APIError(
            "Every restock item must belong to this branch.",
            code="restock_wrong_branch",
        )

    balances = lock_balances(locked.branch, [i.variant_id for i in items])
    now = timezone.now()

    for item in items:
        balance = balances[item.variant_id]
        item.line_total = to_money(Decimal(item.quantity) * item.unit_cost)
        item.save(update_fields=["line_total"])

        balance.average_unit_cost = weighted_average_cost(
            old_quantity=balance.quantity,
            old_average=balance.average_unit_cost,
            received_quantity=item.quantity,
            received_unit_cost=item.unit_cost,
        )
        balance.last_restocked_at = now
        write_movement(
            balance=balance,
            delta=item.quantity,
            movement_type=MovementType.RESTOCK,
            created_by=confirmed_by,
            unit_cost_snapshot=item.unit_cost,
            reference_type="restock",
            reference_id=locked.pk,
        )

    locked.status = RestockStatus.CONFIRMED
    locked.confirmed_by = confirmed_by
    locked.confirmed_at = now
    # Same purchase total as the draft carried — recomputed from the items as
    # the single source of truth.
    locked.total_cost = restock_purchase_total(locked)
    locked.save(
        update_fields=[
            "status",
            "confirmed_by",
            "confirmed_at",
            "total_cost",
            "updated_at",
        ]
    )
    record_audit(
        action="restock.confirm",
        target=locked,
        actor=confirmed_by,
        branch=locked.branch,
        request=request,
        after={"total_cost": str(locked.total_cost), "item_count": len(items)},
    )
    return locked
