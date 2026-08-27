"""Shared serializer bases.

``ControlCharSafeSerializerMixin`` rejects any inbound string (at any depth of
the parsed request body) that contains an unsafe control character. DRF's
``CharField`` already rejects NUL; this closes the rest of the C0/C1 range,
TAB/LF/CR excepted. Legitimate business data never contains these, so nothing
is silently mutated — the request is rejected with the standard envelope.
"""

from __future__ import annotations

import re

from rest_framework import serializers

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _find_control_char(value, path: str = ""):
    if isinstance(value, str):
        if _CONTROL_CHARS.search(value):
            return path or "body"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            hit = _find_control_char(item, f"{path}.{key}" if path else str(key))
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hit = _find_control_char(item, f"{path}[{index}]")
            if hit:
                return hit
    return None


class ControlCharSafeSerializerMixin:
    def to_internal_value(self, data):
        hit = _find_control_char(data)
        if hit is not None:
            raise serializers.ValidationError(
                {hit: ["Control characters are not allowed."]},
                code="control_characters",
            )
        return super().to_internal_value(data)


class ControlCharSafeModelSerializer(
    ControlCharSafeSerializerMixin, serializers.ModelSerializer
):
    pass


class ControlCharSafeSerializer(ControlCharSafeSerializerMixin, serializers.Serializer):
    pass
