"""Authentication helpers wired into django-axes."""

import logging

from django.http import JsonResponse

from apps.core.request_context import get_request_id

security_logger = logging.getLogger("apps.security")


def lockout_response(request, credentials=None, *args, **kwargs):
    """Response returned by django-axes once the failure limit is reached.

    Emits a scrubbed structured security-log line — never the submitted
    password or any credential value, only the username being locked out and
    the client IP so repeated abuse can be investigated.
    """

    username = ""
    if isinstance(credentials, dict):
        username = str(credentials.get("username", ""))[:150]

    security_logger.warning(
        "security_event",
        extra={
            "event": "axes_lockout",
            "code": "too_many_attempts",
            "status": 429,
            "path": getattr(request, "path", None),
            "username": username,
            "client_ip": request.META.get("REMOTE_ADDR") if request else None,
            "request_id": get_request_id(),
        },
    )

    return JsonResponse(
        {
            "code": "too_many_attempts",
            "message": (
                "Too many failed sign-in attempts. Please wait a few minutes "
                "and try again."
            ),
            "field_errors": {},
            "request_id": get_request_id(),
        },
        status=429,
    )
