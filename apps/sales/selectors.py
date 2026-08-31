"""Sales reads with role-aware scoping."""

from __future__ import annotations

from django.db.models import Prefetch

from apps.sales.models import ApprovalRequest, ApprovalType, Sale

# G22 — the newest DISCOUNT approval per sale, with the two usernames joined, so
# a sale list serialising ``discount_request`` never becomes N+1.
_discount_requests_prefetch = Prefetch(
    "approval_requests",
    queryset=ApprovalRequest.objects.filter(request_type=ApprovalType.DISCOUNT)
    .select_related("requested_by", "reviewed_by")
    .order_by("-created_at"),
    to_attr="_discount_requests",
)


def sales_visible_to(user):
    """Owner: every sale in their branch. Employee: only their own sales."""

    queryset = (
        Sale.objects.select_related("cashier", "customer")
        .prefetch_related("items", "payments", _discount_requests_prefetch)
        .order_by("-created_at")
    )
    if getattr(user, "is_tech_admin", False) or user.branch_id is None:
        return queryset.none()
    queryset = queryset.filter(branch_id=user.branch_id)
    if not getattr(user, "is_owner", False):
        queryset = queryset.filter(cashier_id=user.id)
    return queryset
