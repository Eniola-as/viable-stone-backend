"""Product-image upload validation.

Product images are JPEG, PNG or WebP **only** — never PDF. This is deliberately
*not* the shared ``apps.core.validators.validate_private_upload`` used for
expense receipts, which also accepts ``application/pdf``. The real leading bytes
are inspected; the filename and the browser-supplied MIME type are never
trusted, so a PDF (or anything else) renamed ``photo.jpg`` is rejected.
"""

from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import serializers

logger = logging.getLogger("apps.security")

# content_type -> (allowed extensions, accepted magic-byte prefixes)
_IMAGE_SIGNATURES: dict[str, tuple[set[str], tuple[bytes, ...]]] = {
    "image/jpeg": ({"jpg", "jpeg"}, (b"\xff\xd8\xff",)),
    "image/png": ({"png"}, (b"\x89PNG\r\n\x1a\n",)),
    "image/webp": ({"webp"}, (b"RIFF",)),  # bytes 8:12 must additionally be b"WEBP"
}

_MAX_DEFAULT = 5 * 1024 * 1024


def sniff_image_type(head: bytes) -> str | None:
    """Return the image content type implied by the leading bytes, or ``None``."""

    for content_type, (_exts, prefixes) in _IMAGE_SIGNATURES.items():
        if not any(head.startswith(prefix) for prefix in prefixes):
            continue
        if content_type == "image/webp":
            if len(head) >= 12 and head[8:12] == b"WEBP":
                return content_type
            continue
        return content_type
    return None


def validate_product_image(uploaded_file) -> None:
    """Raise ``serializers.ValidationError`` unless *uploaded_file* is a small
    JPEG, PNG or WebP whose real content matches its file extension."""

    max_size = int(getattr(settings, "PRODUCT_IMAGE_MAX_BYTES", _MAX_DEFAULT))
    size = getattr(uploaded_file, "size", None)
    if size == 0:
        raise serializers.ValidationError("The image file is empty.")
    if size is not None and size > max_size:
        _log_rejected("too_large", uploaded_file, None)
        raise serializers.ValidationError(
            f"The image exceeds the {max_size // (1024 * 1024)} MB limit."
        )

    position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    try:
        uploaded_file.seek(0)
        head = uploaded_file.read(32)
    finally:
        uploaded_file.seek(position)
    if isinstance(head, str):  # opened in text mode somehow
        head = head.encode("latin-1", "ignore")

    sniffed = sniff_image_type(head)
    allowed = set(
        getattr(settings, "PRODUCT_IMAGE_ALLOWED_TYPES", list(_IMAGE_SIGNATURES))
    )
    if sniffed is None or sniffed not in allowed:
        _log_rejected("bad_content_type", uploaded_file, sniffed)
        raise serializers.ValidationError(
            "Unsupported image. Upload a JPEG, PNG or WebP file."
        )

    name = (getattr(uploaded_file, "name", "") or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    allowed_extensions = _IMAGE_SIGNATURES[sniffed][0]
    if extension not in allowed_extensions:
        _log_rejected("extension_mismatch", uploaded_file, sniffed)
        raise serializers.ValidationError(
            "The file extension does not match the actual image content."
        )


def _log_rejected(reason: str, uploaded_file, sniffed: str | None) -> None:
    logger.warning(
        "security_event",
        extra={
            "event": "rejected_product_image",
            "code": reason,
            "upload_name": (getattr(uploaded_file, "name", "") or "")[:120],
            "sniffed_type": sniffed,
        },
    )
