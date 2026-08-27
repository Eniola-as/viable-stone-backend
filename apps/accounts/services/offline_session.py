"""Exclusive-offline-session guard.

While a branch has an ACTIVE offline checkout authorization, the online system
must not run stock-decreasing operations for that branch — the authorised
offline device is the single till. Callers that would decrease stock check
:func:`assert_no_active_offline_session` first and surface
``409 offline_session_active``.
"""

from __future__ import annotations

from apps.core.exceptions import Conflict


def active_offline_authorization(branch):
    from apps.accounts.models import (
        OfflineAuthorizationStatus,
        OfflineDeviceAuthorization,
    )

    return (
        OfflineDeviceAuthorization.objects.filter(
            branch=branch, status=OfflineAuthorizationStatus.ACTIVE
        )
        .order_by("-created_at")
        .first()
    )


def assert_no_active_offline_session(branch) -> None:
    if active_offline_authorization(branch) is not None:
        raise Conflict(
            "This branch has an active offline checkout session; online "
            "stock-changing operations are paused until it is ended.",
            code="offline_session_active",
        )
