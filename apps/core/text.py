"""Text-sanitisation helpers.

The API returns user-controlled strings verbatim inside JSON (the frontend must
render them as text — see docs/FRONTEND_HANDOFF.md). Anywhere the *backend*
itself renders those strings — currently only the ReportLab receipt PDF —
they must be escaped first: ReportLab's ``Paragraph`` parses a small XML/HTML
markup dialect, so an unescaped ``&`` breaks generation and ``<b>`` / ``<font>``
inject markup.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_control_chars(value) -> str:
    return _CONTROL_CHARS.sub("", str(value or ""))


def pdf_escape(value) -> str:
    """Make a user string safe to drop into a ReportLab ``Paragraph``.

    Strips control characters, then XML-escapes ``& < >`` so no markup —
    including ``<script>``, event-handler attributes or ``javascript:`` — is
    interpreted or rendered as executable/active content.
    """

    return escape(strip_control_chars(value))
