"""User lifecycle helpers. Accounts are deactivated, never deleted."""

from __future__ import annotations

from django.db import transaction

from apps.accounts.models import Role, User
from apps.core.services.audit import record_audit


@transaction.atomic
def create_staff_user(
    *,
    created_by: User,
    username: str,
    password: str,
    role: str,
    branch,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    request=None,
) -> User:
    user = User(
        username=username,
        role=role,
        branch=branch,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        must_change_password=True,
        mfa_required=role in (Role.OWNER, Role.TECH_ADMIN),
    )
    user.set_password(password)
    user.full_clean(exclude=["password"])
    user.save()
    record_audit(
        action="user.create",
        target=user,
        actor=created_by,
        request=request,
        after={"username": username, "role": role, "branch": str(branch.pk)},
    )
    return user


@transaction.atomic
def set_user_active(*, actor: User, user: User, is_active: bool, request=None) -> User:
    before = user.is_active
    user.is_active = is_active
    user.save(update_fields=["is_active", "updated_at"])
    record_audit(
        action="user.activate" if is_active else "user.deactivate",
        target=user,
        actor=actor,
        request=request,
        before={"is_active": before},
        after={"is_active": is_active},
    )
    return user
