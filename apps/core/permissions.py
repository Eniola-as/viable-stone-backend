"""Server-enforced role and MFA permission classes.

Authorization is enforced here (and in querysets/services), never only in the
frontend.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.models import Role


def mfa_satisfied(request) -> bool:
    """True when the request's user has cleared MFA (or does not need it)."""

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if not user.mfa_required:
        return True
    if request.session.get("mfa_verified"):
        return True
    is_verified = getattr(user, "is_verified", None)
    return bool(is_verified and is_verified())


class IsAuthenticatedAndMFAVerified(BasePermission):
    message = "Multi-factor verification is required for this account."

    def has_permission(self, request, view):
        return mfa_satisfied(request)


class _RolePermission(BasePermission):
    allowed_roles: tuple[str, ...] = ()

    def has_permission(self, request, view):
        return mfa_satisfied(request) and request.user.role in self.allowed_roles


class IsOwner(_RolePermission):
    message = "Only the shop owner may perform this action."
    allowed_roles = (Role.OWNER,)


class IsTechAdmin(_RolePermission):
    message = "Only the technical administrator may perform this action."
    allowed_roles = (Role.TECH_ADMIN,)


class IsOwnerOrTechAdmin(_RolePermission):
    allowed_roles = (Role.OWNER, Role.TECH_ADMIN)


class IsEmployee(_RolePermission):
    allowed_roles = (Role.EMPLOYEE,)


class IsOwnerOrReadOnly(BasePermission):
    """Any authenticated (MFA-cleared) user may read; only the owner may write."""

    message = "Only the shop owner may change this resource."

    def has_permission(self, request, view):
        if not mfa_satisfied(request):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role == Role.OWNER
