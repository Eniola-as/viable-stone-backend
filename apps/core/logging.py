import json
import logging

from apps.core.request_context import get_request_id

# Record attributes that are logging internals, not fields worth emitting.
_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
}

# Substrings that mark an extra field's value as sensitive.
_SENSITIVE = (
    "password",
    "secret",
    "token",
    "cookie",
    "authorization",
    "csrf",
    "database_url",
    "dsn",
    "phone",
    "signing_key",
    "vapid",
    "otp",
    "mfa",
)


class RequestIDFilter(logging.Filter):
    """Injects the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line on stdout — suitable for Railway log capture.

    Never emits passwords, secrets, tokens, cookies or database URLs, even if a
    caller passes them via ``extra=``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            if any(hint in key.lower() for hint in _SENSITIVE):
                value = "[scrubbed]"
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
