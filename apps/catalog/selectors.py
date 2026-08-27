"""Optimised catalogue reads."""

from __future__ import annotations

from apps.catalog.models import PriceHistory, Product, ProductVariant


def current_price(variant: ProductVariant) -> PriceHistory | None:
    return (
        PriceHistory.objects.filter(variant=variant, valid_to__isnull=True)
        .order_by("-valid_from")
        .first()
    )


def variants_for_branch(branch_id, *, active_only=False):
    queryset = (
        ProductVariant.objects.select_related(
            "product", "product__brand", "product__category"
        )
        .filter(product__branch_id=branch_id)
        .prefetch_related("price_history")
    )
    if active_only:
        queryset = queryset.filter(is_active=True, product__is_active=True)
    return queryset


def products_for_branch(branch_id, *, active_only=False):
    queryset = (
        Product.objects.select_related("brand", "category")
        .filter(branch_id=branch_id)
        .prefetch_related("variants")
    )
    if active_only:
        queryset = queryset.filter(is_active=True)
    return queryset
