"""Cross-cutting request middleware: request ids and lightweight activity stamps."""

import uuid
from datetime import timedelta

from django.utils import timezone

from apps.core.request_context import set_request_id

REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"
REQUEST_ID_RESPONSE_HEADER = "X-Request-ID"
_ACTIVITY_REFRESH = timedelta(minutes=5)


def _safe_incoming_id(raw: str | None) -> str:
    """Accept a client-supplied request id only if it is a well-formed UUID.

    This keeps log/audit request ids un-forgeable and free of injected content.
    """

    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return ""


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _safe_incoming_id(request.META.get(REQUEST_ID_META_KEY)) or str(
            uuid.uuid4()
        )
        request.request_id = request_id
        set_request_id(request_id)
        response = self.get_response(request)
        response[REQUEST_ID_RESPONSE_HEADER] = request_id
        return response


class ActivityTrackingMiddleware:
    """Stamps ``User.last_activity_at`` at most once every few minutes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return response
        now = timezone.now()
        last = getattr(user, "last_activity_at", None)
        if last is None or now - last > _ACTIVITY_REFRESH:
            from django.contrib.auth import get_user_model

            get_user_model().objects.filter(pk=user.pk).update(last_activity_at=now)
        return response
