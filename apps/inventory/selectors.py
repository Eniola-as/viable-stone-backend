"""Inventory reads: balances, low stock, stock value, movement ledger."""

from __future__ import annotations

from django.db.models import DecimalField, ExpressionWrapper, F, Sum

from apps.inventory.models import InventoryBalance, StockMovement


def balances_for_branch(branch_id):
    return (
        InventoryBalance.objects.select_related(
            "variant", "variant__product", "variant__product__brand"
        )
        .filter(branch_id=branch_id)
        .order_by("variant__sku")
    )


def low_stock_for_branch(branch_id):
    return (
        balances_for_branch(branch_id)
        .filter(variant__low_stock_level__gt=0)
        .filter(quantity__lte=F("variant__low_stock_level"))
    )


def stock_value_for_branch(branch_id):
    line_value = ExpressionWrapper(
        F("quantity") * F("average_unit_cost"),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )
    aggregate = (
        InventoryBalance.objects.filter(branch_id=branch_id)
        .annotate(line_value=line_value)
        .aggregate(total=Sum("line_value"))
    )
    return aggregate["total"] or 0


def movements_for_variant(branch_id, variant_id):
    return (
        StockMovement.objects.filter(branch_id=branch_id, variant_id=variant_id)
        .select_related("created_by")
        .order_by("-created_at")
    )
