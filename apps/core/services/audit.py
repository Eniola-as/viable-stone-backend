"""Single entry point for writing audit rows."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from apps.core.models import AuditLog
from apps.core.request_context import get_request_id


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _json_safe(value):
    """Coerce a ``before`` / ``after`` snapshot to JSON-native types.

    Serializer ``.data`` legitimately contains ``UUID`` (related pks),
    ``Decimal`` (money) and date/datetime values; the ``AuditLog`` JSONField
    cannot store those directly. Convert them rather than let a normal write
    raise a 500.
    """

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


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
        before=_json_safe(before or {}),
        after=_json_safe(after or {}),
    )
