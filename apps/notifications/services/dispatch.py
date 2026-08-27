"""The one entry point for raising a notification.

Transaction boundary:

* The durable in-app :class:`Notification` row is created (and de-duplicated) in
  the **same transaction as the business event** — sale, stock movement, return,
  approval or adjustment. If that transaction rolls back, the row disappears
  with it.
* ``(recipient, dedupe_key)`` is unique. A repeat or concurrent delivery of the
  same logical event collides on that constraint; the collision is absorbed in a
  savepoint so it never poisons the surrounding transaction.
* Only the **external Web Push** delivery is deferred with
  ``transaction.on_commit`` — it runs after the business transaction has
  committed, and any push failure is swallowed so it can never affect it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from django.db import IntegrityError, transaction

from apps.notifications.models import Notification, PushSubscription
from apps.notifications.services import webpush

logger = logging.getLogger("apps.notifications")

Recipients = Iterable | Callable[[], Iterable]


def _resolve_recipients(recipients: Recipients) -> list:
    people = recipients() if callable(recipients) else list(recipients)
    unique: dict = {}
    for user in people:
        if user is not None and user.pk not in unique:
            unique[user.pk] = user
    return list(unique.values())


def _persist(
    recipients,
    *,
    branch,
    notification_type,
    title,
    message,
    action_path,
    related_object_type,
    related_object_id,
    dedupe_key,
) -> list[Notification]:
    """Create one row per recipient in the caller's transaction, deduplicated.

    Each ``get_or_create`` runs in its own savepoint so a unique-constraint race
    on ``(recipient, dedupe_key)`` rolls back only that savepoint, leaving the
    surrounding business transaction healthy.
    """

    created: list[Notification] = []
    for user in recipients:
        try:
            with transaction.atomic():
                obj, is_new = Notification.objects.get_or_create(
                    recipient=user,
                    dedupe_key=dedupe_key,
                    defaults={
                        "branch": branch,
                        "notification_type": notification_type,
                        "title": title[:140],
                        "message": message[:500],
                        "action_path": (action_path or "")[:200],
                        "related_object_type": (related_object_type or "")[:40],
                        "related_object_id": related_object_id,
                    },
                )
        except IntegrityError:
            # Lost a concurrent race for the same (recipient, dedupe_key).
            continue
        if is_new:
            created.append(obj)
    return created


def _push(notification_ids: list) -> None:
    """Best-effort browser push. Never raises."""

    try:
        notifications = list(
            Notification.objects.filter(pk__in=notification_ids).select_related(
                "recipient"
            )
        )
        for note in notifications:
            subs = PushSubscription.objects.filter(
                user_id=note.recipient_id, is_active=True
            )
            for sub in subs:
                webpush.deliver(sub, note)
    except Exception:  # a push problem must never surface after commit
        logger.exception("web push fan-out failed")


def enqueue(
    *,
    recipients: Recipients,
    branch,
    notification_type: str,
    title: str,
    message: str,
    dedupe_key: str,
    action_path: str = "",
    related_object_type: str = "",
    related_object_id=None,
) -> list[Notification]:
    """Write the durable notification row(s) now; schedule Web Push for commit.

    ``recipients`` may be an iterable of users or a zero-arg callable returning
    one. Returns the rows that were newly created.
    """

    people = _resolve_recipients(recipients)
    if not people:
        return []

    created = _persist(
        people,
        branch=branch,
        notification_type=notification_type,
        title=title,
        message=message,
        action_path=action_path,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        dedupe_key=dedupe_key,
    )
    if created:
        created_ids = [note.pk for note in created]
        transaction.on_commit(lambda: _push(created_ids))
    return created
