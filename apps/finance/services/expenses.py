"""Expense lifecycle. An incorrect expense is voided (with a reason), never edited
or deleted — its original amount stays on record and drops out of net profit.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import APIError, Conflict
from apps.core.services.audit import record_audit
from apps.finance.models import Expense


@transaction.atomic
def void_expense(*, expense: Expense, actor, reason: str, request=None) -> Expense:
    reason = (reason or "").strip()
    if not reason:
        raise APIError(
            "A void reason is required.",
            code="void_reason_required",
            field_errors={"reason": ["This field is required."]},
        )

    locked = Expense.objects.select_for_update().get(pk=expense.pk)
    if locked.is_voided:
        raise Conflict("This expense is already voided.", code="expense_already_voided")

    locked.is_voided = True
    locked.void_reason = reason
    locked.voided_by = actor
    locked.voided_at = timezone.now()
    locked.save(
        update_fields=[
            "is_voided",
            "void_reason",
            "voided_by",
            "voided_at",
            "updated_at",
        ]
    )
    record_audit(
        action="expense.void",
        target=locked,
        actor=actor,
        branch=locked.branch,
        request=request,
        before={"is_voided": False},
        after={"is_voided": True, "reason": reason, "amount": str(locked.amount)},
    )
    return locked
