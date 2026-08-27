"""Notifications for the owner-approval workflow (discounts and returns).

* An employee submitting a discount or return request notifies the branch's
  active owners (never the requester).
* The owner's decision notifies the original requester — unless they are the
  same person, in which case nothing is sent (own action, no response needed).
"""

from __future__ import annotations

from apps.notifications.action_paths import action_path
from apps.notifications.models import NotificationType
from apps.notifications.selectors import active_branch_owners
from apps.notifications.services.dispatch import enqueue

_KIND = {
    "DISCOUNT": "discount",
    "RETURN": "return",
}


def _kind(approval) -> str:
    return _KIND.get(approval.request_type, "approval")


def notify_approval_requested(approval) -> None:
    kind = _kind(approval)
    branch = approval.branch
    requester_id = approval.requested_by_id

    def _recipients():
        return [
            owner for owner in active_branch_owners(branch) if owner.pk != requester_id
        ]

    enqueue(
        recipients=_recipients,
        branch=branch,
        notification_type=NotificationType.APPROVAL_REQUESTED,
        title=f"{kind.capitalize()} approval requested",
        message=f"A {kind} approval was requested and is awaiting your decision.",
        dedupe_key=f"APPROVAL_REQUESTED:{approval.id}",
        action_path=action_path("approval", approval_id=approval.id),
        related_object_type="approval",
        related_object_id=approval.id,
    )


def notify_approval_decided(approval, *, approved: bool) -> None:
    kind = _kind(approval)
    branch = approval.branch
    requester = approval.requested_by
    reviewer_id = approval.reviewed_by_id
    ntype = (
        NotificationType.APPROVAL_APPROVED
        if approved
        else NotificationType.APPROVAL_REJECTED
    )
    verb = "approved" if approved else "rejected"

    def _recipients():
        if requester is None or not requester.is_active:
            return []
        if requester.pk == reviewer_id:
            return []
        return [requester]

    enqueue(
        recipients=_recipients,
        branch=branch,
        notification_type=ntype,
        title=f"{kind.capitalize()} request {verb}",
        message=f"Your {kind} request was {verb} by the owner.",
        dedupe_key=f"{ntype}:{approval.id}",
        action_path=action_path("approval", approval_id=approval.id),
        related_object_type="approval",
        related_object_id=approval.id,
    )
