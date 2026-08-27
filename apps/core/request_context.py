"""Per-request context (currently just the request id) shared with logging."""

import contextvars

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def set_request_id(value: str) -> None:
    _request_id.set(value or "-")


def get_request_id() -> str:
    return _request_id.get()
