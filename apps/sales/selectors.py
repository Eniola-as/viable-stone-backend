"""Sales reads with role-aware scoping."""

from __future__ import annotations

from apps.sales.models import Sale


def sales_visible_to(user):
    """Owner: every sale in their branch. Employee: only their own sales."""

    queryset = (
        Sale.objects.select_related("cashier", "customer")
        .prefetch_related("items", "payments")
        .order_by("-created_at")
    )
    if getattr(user, "is_tech_admin", False) or user.branch_id is None:
        return queryset.none()
    queryset = queryset.filter(branch_id=user.branch_id)
    if not getattr(user, "is_owner", False):
        queryset = queryset.filter(cashier_id=user.id)
    return queryset
