"""drf-spectacular post-processing: publish the standard error envelope (G1).

The API has always answered every non-2xx with one shape
``{code, message, field_errors, request_id}`` (see
``apps.core.exceptions.api_exception_handler``), but that lived only in prose.
This hook adds a reusable ``Error`` schema + a ``components.responses.Error``
entry and attaches it as the ``default`` response of every operation, so a
generated client has one typed error model and one place to look.

It invents no per-endpoint error lists — ``default`` is the OpenAPI idiom for
"any other status uses this shape".
"""

from __future__ import annotations

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

ERROR_SCHEMA = {
    "type": "object",
    "description": (
        "Standard error envelope. Every non-2xx response has this shape. "
        "`code` is a stable machine-readable string that frontend logic may "
        "branch on; `message` is human-readable and may change; `field_errors` "
        "maps a field name to its messages on a 400 validation error (empty "
        "otherwise); `request_id` echoes `X-Request-ID` for support. Internal "
        "exception names, stack traces and secrets are never included."
    ),
    "properties": {
        "code": {"type": "string", "example": "validation_error"},
        "message": {"type": "string"},
        "field_errors": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "request_id": {"type": "string"},
    },
    "required": ["code", "message", "field_errors", "request_id"],
}


def add_error_envelope(result, generator, request, public):
    components = result.setdefault("components", {})
    components.setdefault("schemas", {})["Error"] = ERROR_SCHEMA
    components.setdefault("responses", {})["Error"] = {
        "description": (
            "Error envelope — 400/401/403/404/405/409/413/415/422/429/500 all "
            "use this shape. See the `Error` schema and FRONTEND_HANDOFF.md for "
            "the stable `code` catalogue."
        ),
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
        },
    }
    default_ref = {"$ref": "#/components/responses/Error"}
    for path_item in result.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation.setdefault("responses", {}).setdefault("default", default_ref)
    return result
