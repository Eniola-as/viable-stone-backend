"""Applying a physical stock count as approved ADJUSTMENT movements."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import APIError, Conflict
from apps.core.services.audit import record_audit
from apps.inventory.models import MovementType, StockCount, StockCountStatus
from apps.inventory.services.stock import lock_balances, write_movement


@transaction.atomic
def submit_stock_count(*, stock_count: StockCount, actor, request=None) -> StockCount:
    locked = StockCount.objects.select_for_update().get(pk=stock_count.pk)
    if locked.status != StockCountStatus.DRAFT:
        raise Conflict(
            "Only a draft stock count can be submitted.",
            code="stock_count_not_draft",
        )
    if not locked.items.exists():
        raise APIError("Add counted items first.", code="stock_count_empty")
    locked.status = StockCountStatus.SUBMITTED
    locked.submitted_at = timezone.now()
    locked.save(update_fields=["status", "submitted_at", "updated_at"])
    record_audit(
        action="stock_count.submit", target=locked, actor=actor, request=request
    )
    return locked


@transaction.atomic
def apply_stock_count(
    *, stock_count: StockCount, applied_by, request=None
) -> StockCount:
    """Reconcile system quantities to the counted quantities via ADJUSTMENT rows.

    Never silently overwrites the ledger — every difference is one movement.
    """

    locked = (
        StockCount.objects.select_for_update()
        .select_related("branch")
        .get(pk=stock_count.pk)
    )
    if locked.status == StockCountStatus.APPLIED:
        raise Conflict(
            "This stock count has already been applied.",
            code="stock_count_already_applied",
        )
    if locked.status == StockCountStatus.CANCELLED:
        raise Conflict("This stock count was cancelled.", code="stock_count_cancelled")

    items = list(locked.items.select_related("variant").order_by("variant_id"))
    if not items:
        raise APIError("This stock count has no items.", code="stock_count_empty")

    balances = lock_balances(locked.branch, [i.variant_id for i in items])
    adjustments = 0
    for item in items:
        balance = balances[item.variant_id]
        delta = int(item.counted_quantity) - int(balance.quantity)
        item.system_quantity_snapshot = balance.quantity
        item.variance = delta
        item.save(update_fields=["system_quantity_snapshot", "variance"])
        if delta != 0:
            write_movement(
                balance=balance,
                delta=delta,
                movement_type=MovementType.ADJUSTMENT,
                created_by=applied_by,
                reference_type="stock_count",
                reference_id=locked.pk,
                reason=locked.reason,
            )
            adjustments += 1

    locked.status = StockCountStatus.APPLIED
    locked.applied_by = applied_by
    locked.applied_at = timezone.now()
    locked.save(update_fields=["status", "applied_by", "applied_at", "updated_at"])
    record_audit(
        action="stock_count.apply",
        target=locked,
        actor=applied_by,
        branch=locked.branch,
        request=request,
        after={"adjusted_variants": adjustments, "reason": locked.reason},
    )
    return locked
