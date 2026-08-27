"""Server-side validation of user-uploaded private files.

File extensions are never trusted: the leading bytes are sniffed and must match
both an allowed content type *and* the declared extension. Used for expense
receipts and any other private document upload.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

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
        raise serializers.ValidationError(
            "Unsupported file. Upload a JPEG, PNG, WebP or PDF document."
        )

    name = (getattr(uploaded_file, "name", "") or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    allowed_extensions = next(
        exts for content_type, exts, _ in _SIGNATURES if content_type == sniffed
    )
    if extension not in allowed_extensions:
        raise serializers.ValidationError(
            "The file extension does not match its actual content."
        )
