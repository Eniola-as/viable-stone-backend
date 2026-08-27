"""Consistent API error envelope.

Every error response is::

    {
        "code": "stock_not_available",
        "message": "Only 3 units are available.",
        "field_errors": {},
        "request_id": "unique-reference"
    }
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions as drf_exc
from rest_framework.response import Response
from rest_framework.settings import api_settings

from apps.core.request_context import get_request_id

logger = logging.getLogger("apps.core.exceptions")

_NON_FIELD_KEYS = {"non_field_errors", api_settings.NON_FIELD_ERRORS_KEY}


class APIError(drf_exc.APIException):
    """Raise inside services/views for a controlled, coded error response."""

    status_code = 400
    default_code = "error"
    default_detail = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        field_errors: dict | None = None,
    ):
        self.code = code or self.default_code
        self.message = message or self.default_detail
        self.field_errors = field_errors or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message, self.code)


class Conflict(APIError):
    status_code = 409
    default_code = "conflict"
    default_detail = "The request conflicts with the current state."


def _from_validation_error(exc: drf_exc.ValidationError) -> tuple[str, str, dict]:
    field_errors: dict[str, list[str]] = {}
    non_field: list[str] = []
    detail = exc.detail
    if isinstance(detail, dict):
        for key, value in detail.items():
            messages = value if isinstance(value, list) else [value]
            messages = [str(m) for m in messages]
            if key in _NON_FIELD_KEYS:
                non_field.extend(messages)
            else:
                field_errors[str(key)] = messages
    elif isinstance(detail, list):
        non_field.extend(str(m) for m in detail)
    else:
        non_field.append(str(detail))
    message = non_field[0] if non_field else "One or more fields are invalid."
    return "validation_error", message, field_errors


_CODE_ALIASES = {
    "not_authenticated": "not_authenticated",
    "authentication_failed": "authentication_failed",
    "permission_denied": "permission_denied",
    "not_found": "not_found",
    "throttled": "throttled",
    "method_not_allowed": "method_not_allowed",
    "parse_error": "parse_error",
    "unsupported_media_type": "unsupported_media_type",
}


def _normalise(exc, response) -> tuple[str, str, dict]:
    if isinstance(exc, APIError):
        return exc.code, exc.message, exc.field_errors
    if isinstance(exc, drf_exc.ValidationError):
        return _from_validation_error(exc)

    data = response.data
    if isinstance(data, dict) and "detail" in data:
        message = str(data["detail"])
    elif isinstance(data, list):
        message = "; ".join(str(m) for m in data)
    elif isinstance(data, dict):
        message = "; ".join(f"{k}: {v}" for k, v in data.items())
    else:
        message = str(data)

    default_code = getattr(exc, "default_code", None) or "error"
    return _CODE_ALIASES.get(default_code, default_code), message, {}


def api_exception_handler(exc, context):
    # Imported lazily: rest_framework.views imports settings that reference this
    # module's siblings, so a top-level import would be circular at startup.
    from rest_framework.views import exception_handler as drf_default_handler

    if isinstance(exc, Http404):
        exc = drf_exc.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exc.PermissionDenied()
    elif isinstance(exc, DjangoValidationError):
        exc = drf_exc.ValidationError(detail=getattr(exc, "message_dict", exc.messages))

    response = drf_default_handler(exc, context)
    request_id = get_request_id()

    if response is None:
        # Unhandled exception -> log the full trace server-side, return a
        # generic 500 to the client with no traceback or internals.
        logger.exception("Unhandled API exception [request_id=%s]", request_id)
        return Response(
            {
                "code": "server_error",
                "message": (
                    "A server error occurred. Quote the request id when "
                    "contacting support."
                ),
                "field_errors": {},
                "request_id": request_id,
            },
            status=500,
        )

    code, message, field_errors = _normalise(exc, response)
    headers = {
        k: response[k]
        for k in ("Retry-After", "WWW-Authenticate", "Allow")
        if k in response
    }
    response.data = {
        "code": code,
        "message": message,
        "field_errors": field_errors,
        "request_id": request_id,
    }
    for key, value in headers.items():
        response[key] = value
    return response
