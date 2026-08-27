"""Authentication helpers wired into django-axes."""

from django.http import JsonResponse

from apps.core.request_context import get_request_id


def lockout_response(request, credentials=None, *args, **kwargs):
    """Response returned by django-axes once the failure limit is reached."""

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
