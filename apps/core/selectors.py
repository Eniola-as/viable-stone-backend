"""Reusable read queries for core models."""

from apps.core.models import AuditLog


def audit_logs_for_user(user):
    """Audit rows visible to ``user``.

    Owners see their branch's rows; the technical administrator sees every row
    (technical/recovery oversight). Employees see nothing.
    """

    queryset = AuditLog.objects.select_related("actor", "branch").order_by(
        "-created_at"
    )
    if getattr(user, "is_tech_admin", False):
        return queryset
    if getattr(user, "is_owner", False) and user.branch_id is not None:
        return queryset.filter(branch_id=user.branch_id)
    return queryset.none()
