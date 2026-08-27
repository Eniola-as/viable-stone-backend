"""Single entry point for writing audit rows."""

from __future__ import annotations

import uuid

from apps.core.models import AuditLog
from apps.core.request_context import get_request_id


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def record_audit(
    *,
    action: str,
    target=None,
    target_type: str | None = None,
    target_id=None,
    actor=None,
    branch=None,
    before: dict | None = None,
    after: dict | None = None,
    request=None,
) -> AuditLog:
    """Append one audit row.

    Never pass secrets, recovery codes, TOTP seeds, full customer phone numbers
    or raw file bytes in ``before`` / ``after``.
    """

    if target is not None:
        target_type = target_type or target.__class__.__name__
        if target_id is None:
            target_id = getattr(target, "pk", None)

    request_id = None
    if request is not None:
        request_id = getattr(request, "request_id", None)
        if actor is None:
            candidate = getattr(request, "user", None)
            if candidate is not None and candidate.is_authenticated:
                actor = candidate
    request_id = request_id or get_request_id()

    if branch is None and actor is not None:
        branch = getattr(actor, "branch", None)

    return AuditLog.objects.create(
        action=action,
        target_type=target_type or "",
        target_id=_as_uuid(target_id),
        actor=actor,
        branch=branch,
        request_id=_as_uuid(request_id),
        before=before or {},
        after=after or {},
    )
