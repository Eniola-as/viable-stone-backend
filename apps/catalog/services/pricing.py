"""Selling-price workflow. Historical price rows are immutable."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import PriceHistory, ProductVariant
from apps.core.exceptions import APIError
from apps.core.services.audit import record_audit


@transaction.atomic
def set_active_price(
    *,
    variant: ProductVariant,
    amount: Decimal,
    changed_by,
    request=None,
) -> PriceHistory:
    """Close the variant's current price and open a new one, atomically.

    Owner-only (enforced by the calling view's permission class).
    """

    if amount is None or Decimal(amount) <= 0:
        raise APIError(
            "A selling price must be greater than zero.",
            code="invalid_price",
            field_errors={"amount": ["Must be greater than zero."]},
        )

    # Lock this variant's price rows so two concurrent changes can't both open
    # a "current" row.
    locked_current = (
        PriceHistory.objects.select_for_update()
        .filter(variant=variant, valid_to__isnull=True)
        .first()
    )
    now = timezone.now()

    previous_amount = None
    if locked_current is not None:
        if locked_current.amount == Decimal(amount):
            return locked_current
        previous_amount = locked_current.amount
        locked_current.valid_to = now
        locked_current.save(update_fields=["valid_to"])

    new_row = PriceHistory.objects.create(
        variant=variant,
        amount=amount,
        valid_from=now,
        changed_by=changed_by,
    )
    record_audit(
        action="price.set",
        target=variant,
        actor=changed_by,
        branch=variant.product.branch,
        request=request,
        before={
            "amount": str(previous_amount) if previous_amount is not None else None
        },
        after={"amount": str(new_row.amount)},
    )
    return new_row
