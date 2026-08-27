"""Helpers to stand up a priced, stocked variant for sales tests."""

from __future__ import annotations

from decimal import Decimal

from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.inventory.services.stock import open_stock


def stocked_variant(
    branch, owner, *, price, quantity, unit_cost="100.00", **variant_kw
):
    """Create an active variant in ``branch`` with a current price and opening stock."""

    from apps.catalog.services.pricing import set_active_price

    variant = ProductVariantFactory(
        product=ProductFactory(branch=branch, is_active=True),
        is_active=True,
        **variant_kw,
    )
    set_active_price(variant=variant, amount=Decimal(str(price)), changed_by=owner)
    if quantity:
        open_stock(
            branch=branch,
            variant=variant,
            quantity=quantity,
            unit_cost=Decimal(str(unit_cost)),
            created_by=owner,
        )
    return variant
