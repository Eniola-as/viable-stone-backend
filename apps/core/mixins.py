"""Reusable view mixins for branch scoping and audit."""

from apps.core.services.audit import record_audit


class BranchScopedQuerysetMixin:
    """Restrict list/detail querysets to the caller's branch.

    - Owners and employees only ever see their own branch's rows, so a guessed
      object id from another branch returns 404, not another branch's data.
    - The technical administrator is not a business actor; by default they see
      nothing here unless ``tech_admin_sees_all`` is set on the view.
    """

    branch_lookup = "branch_id"
    tech_admin_sees_all = False

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if getattr(user, "is_tech_admin", False):
            return queryset if self.tech_admin_sees_all else queryset.none()
        if user.branch_id is None:
            return queryset.none()
        return queryset.filter(**{self.branch_lookup: user.branch_id})


class AuditCreateUpdateMixin:
    """Write an audit row after create/update/destroy on a viewset."""

    audit_action_prefix = None

    def _audit_prefix(self):
        return self.audit_action_prefix or self.queryset.model.__name__.lower()

    def perform_create(self, serializer):
        instance = serializer.save()
        record_audit(
            action=f"{self._audit_prefix()}.create",
            target=instance,
            request=self.request,
            after=_safe_dict(serializer.data),
        )

    def perform_update(self, serializer):
        before = _safe_dict(self.get_serializer(serializer.instance).data)
        instance = serializer.save()
        record_audit(
            action=f"{self._audit_prefix()}.update",
            target=instance,
            request=self.request,
            before=before,
            after=_safe_dict(serializer.data),
        )

    def perform_destroy(self, instance):
        record_audit(
            action=f"{self._audit_prefix()}.delete",
            target=instance,
            request=self.request,
            before={"id": str(instance.pk)},
        )
        instance.delete()


_SENSITIVE_KEYS = {
    "password",
    "code",
    "code_hash",
    "token",
    "secret",
    "otp",
    "recovery_codes",
    "phone",
}


def _safe_dict(data):
    """Drop obviously sensitive keys before an audit snapshot is stored."""

    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items() if key.lower() not in _SENSITIVE_KEYS
    }
