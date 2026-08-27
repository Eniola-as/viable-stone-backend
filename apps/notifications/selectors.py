"""Recipient lookups for notifications."""

from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.accounts.models import Role


def active_branch_owners(branch) -> list:
    """Every active owner attached to ``branch``.

    Disabled accounts are never notified.
    """

    user_model = get_user_model()
    return list(
        user_model.objects.filter(
            branch=branch, role=Role.OWNER, is_active=True
        ).order_by("date_joined", "id")
    )
