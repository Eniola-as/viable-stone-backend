"""Best-effort Web Push delivery.

Isolation guarantees:

* :func:`deliver` never raises — a push failure must never roll back or break
  the durable in-app notification that was already written.
* No network call happens unless ``settings.PUSH_ENABLED`` is true (it is false
  in tests and whenever VAPID keys are absent), so automated tests never reach
  a real push service.
* A subscription the push service permanently rejects (HTTP 404 / 410) or that
  fails repeatedly is deactivated automatically.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

logger = logging.getLogger("apps.notifications.webpush")

# Push-service status codes meaning "this subscription is gone for good".
_PERMANENT_REJECT = {404, 410}
_MAX_TRANSIENT_FAILURES = 5


def _payload(note) -> str:
    return json.dumps(
        {
            "type": note.notification_type,
            "title": note.title,
            "body": note.message,
            "path": note.action_path,
            "id": str(note.pk),
        }
    )


def _record_success(subscription) -> None:
    subscription.last_used_at = timezone.now()
    subscription.failure_count = 0
    subscription.save(update_fields=["last_used_at", "failure_count", "updated_at"])


def _record_failure(subscription, *, permanent: bool) -> None:
    if permanent:
        subscription.deactivate(expired=True)
        return
    subscription.failure_count = (subscription.failure_count or 0) + 1
    if subscription.failure_count >= _MAX_TRANSIENT_FAILURES:
        subscription.deactivate(expired=True)
    else:
        subscription.save(update_fields=["failure_count", "updated_at"])


def deliver(subscription, note) -> bool:
    """Push one notification to one subscription. Returns True only on success."""

    if not getattr(settings, "PUSH_ENABLED", False):
        return False
    if not subscription.is_active:
        return False

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {
                    "p256dh": subscription.p256dh,
                    "auth": subscription.auth,
                },
            },
            data=_payload(note),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
            ttl=getattr(settings, "PUSH_DEFAULT_TTL", 600),
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        _record_failure(subscription, permanent=status in _PERMANENT_REJECT)
        # Log only non-secret context: never the exception text (it can echo the
        # request), the subscription keys or the VAPID private key.
        logger.warning(
            "web push rejected: sub=%s status=%s type=%s",
            subscription.pk,
            status,
            type(exc).__name__,
        )
        return False
    except Exception as exc:  # never let a push error escape
        logger.warning(
            "web push error: sub=%s type=%s", subscription.pk, type(exc).__name__
        )
        return False

    _record_success(subscription)
    return True
