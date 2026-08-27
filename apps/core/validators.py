"""Server-side validation of user-supplied text and uploaded private files.

File extensions are never trusted: the leading bytes are sniffed and must match
both an allowed content type *and* the declared extension. Used for expense
receipts and any other private document upload.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.deconstruct import deconstructible
from rest_framework import serializers

logger = logging.getLogger("apps.security")

# C0 control characters except TAB / LF / CR, plus DEL and the C1 range. DRF's
# CharField already rejects NUL (U+0000) via ProhibitNullCharactersValidator;
# this covers the rest, which have no place in business text and are a common
# vector for log-forging, terminal-escape and parser-confusion attacks.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


@deconstructible
class NoControlCharactersValidator:
    """Reject strings containing unsafe control characters."""

    message = "Control characters are not allowed."
    code = "control_characters"

    def __call__(self, value):
        if value and _CONTROL_CHARS.search(str(value)):
            raise DjangoValidationError(self.message, code=self.code)

    def __eq__(self, other):
        return isinstance(other, NoControlCharactersValidator)


no_control_characters = NoControlCharactersValidator()

# (content_type, {allowed extensions}, [magic-byte prefixes])
_SIGNATURES: list[tuple[str, set[str], list[bytes]]] = [
    ("image/jpeg", {"jpg", "jpeg"}, [b"\xff\xd8\xff"]),
    ("image/png", {"png"}, [b"\x89PNG\r\n\x1a\n"]),
    ("image/webp", {"webp"}, [b"RIFF"]),  # plus b"WEBP" at offset 8
    ("application/pdf", {"pdf"}, [b"%PDF-"]),
]


def sniff_content_type(head: bytes) -> str | None:
    for content_type, _exts, prefixes in _SIGNATURES:
        for prefix in prefixes:
            if not head.startswith(prefix):
                continue
            if content_type == "image/webp":
                if len(head) >= 12 and head[8:12] == b"WEBP":
                    return content_type
                continue
            return content_type
    return None


def validate_private_upload(uploaded_file) -> None:
    """Raise ``serializers.ValidationError`` unless the file is a small,
    allowed private document whose real content matches its extension."""

    max_size = int(getattr(settings, "MAX_UPLOAD_SIZE", 5 * 1024 * 1024))
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > max_size:
        _log_rejected_upload("too_large", getattr(uploaded_file, "name", ""), None)
        raise serializers.ValidationError(
            f"The file exceeds the {max_size // (1024 * 1024)} MB limit."
        )

    position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    try:
        uploaded_file.seek(0)
        head = uploaded_file.read(2048)
    finally:
        uploaded_file.seek(position)
    if isinstance(head, str):  # opened in text mode somehow
        head = head.encode("latin-1", "ignore")

    sniffed = sniff_content_type(head)
    allowed_types = set(getattr(settings, "ALLOWED_UPLOAD_MIME_TYPES", []))
    if sniffed is None or sniffed not in allowed_types:
        _log_rejected_upload(
            "bad_content_type", getattr(uploaded_file, "name", ""), sniffed
        )
        raise serializers.ValidationError(
            "Unsupported file. Upload a JPEG, PNG, WebP or PDF document."
        )

    name = (getattr(uploaded_file, "name", "") or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    allowed_extensions = next(
        exts for content_type, exts, _ in _SIGNATURES if content_type == sniffed
    )
    if extension not in allowed_extensions:
        _log_rejected_upload("extension_mismatch", name, sniffed)
        raise serializers.ValidationError(
            "The file extension does not match its actual content."
        )


def _log_rejected_upload(reason: str, name: str, sniffed: str | None) -> None:
    logger.warning(
        "security_event",
        extra={
            "event": "rejected_upload",
            "code": reason,
            "upload_name": (name or "")[:120],
            "sniffed_type": sniffed,
        },
    )
