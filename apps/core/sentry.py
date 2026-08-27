"""Optional Sentry integration — completely inert without ``SENTRY_DSN``.

``send_default_pii`` is off and ``before_send`` strips cookies, auth/CSRF
headers, request bodies and any value that looks like a customer phone, a
payment reference, a push key, an offline token, a secret or a database URL.
The request id is preserved as a tag so a Sentry event still correlates with
the structured logs.
"""

from __future__ import annotations

from apps.core.request_context import get_request_id

_SCRUB = "[scrubbed]"

# Header names (lower-cased) that must never reach Sentry.
_SENSITIVE_HEADERS = {
    "cookie",
    "authorization",
    "x-csrftoken",
    "x-request-id",
    "proxy-authorization",
}

# Substrings in a key name that mark its value as sensitive.
_SENSITIVE_KEY_HINTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "csrf",
    "cookie",
    "phone",
    "reference",
    "p256dh",
    "auth",
    "signed_token",
    "database_url",
    "dsn",
    "otp",
    "mfa",
    "recovery",
    "vapid",
    "signing_key",
)


def _looks_sensitive(key: str) -> bool:
    key = key.lower()
    return any(hint in key for hint in _SENSITIVE_KEY_HINTS)


def _scrub_mapping(data):
    if not isinstance(data, dict):
        return data
    cleaned = {}
    for key, value in data.items():
        if _looks_sensitive(str(key)):
            cleaned[key] = _SCRUB
        elif isinstance(value, dict):
            cleaned[key] = _scrub_mapping(value)
        else:
            cleaned[key] = value
    return cleaned


def scrub_event(event, hint=None):
    """``before_send`` hook. Returns the sanitised event (never ``None``)."""

    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                name: (_SCRUB if name.lower() in _SENSITIVE_HEADERS else value)
                for name, value in headers.items()
            }
        request.pop("cookies", None)
        # Never ship request bodies / query payloads.
        request.pop("data", None)
        request.pop("query_string", None)

    for section in ("extra", "contexts", "tags"):
        if isinstance(event.get(section), dict):
            event[section] = _scrub_mapping(event[section])

    tags = event.setdefault("tags", {})
    if isinstance(tags, dict):
        tags.setdefault("request_id", get_request_id())

    if "user" in event and isinstance(event["user"], dict):
        event["user"] = {
            k: v for k, v in event["user"].items() if k in {"id", "username"}
        }
    return event


def build_sentry_kwargs(env) -> dict | None:
    """Return kwargs for ``sentry_sdk.init`` — or ``None`` when no DSN is set."""

    dsn = env("SENTRY_DSN", default="")
    if not dsn:
        return None
    return {
        "dsn": dsn,
        "environment": env("SENTRY_ENVIRONMENT", default="production"),
        "release": env("SENTRY_RELEASE", default="") or None,
        "traces_sample_rate": env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.05),
        "send_default_pii": False,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }


def configure_sentry(env) -> bool:
    """Initialise Sentry if a DSN is present. Returns whether it was enabled."""

    kwargs = build_sentry_kwargs(env)
    if kwargs is None:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(integrations=[DjangoIntegration()], **kwargs)
    return True
