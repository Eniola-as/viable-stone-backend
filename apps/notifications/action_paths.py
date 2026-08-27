"""Server-side allowlist for notification / push deep links.

Push payload action paths must be generated here from a fixed set of templates,
never from an arbitrary user-supplied URL.
"""

from __future__ import annotations

_BUILDERS = {
    "approval": lambda approval_id: f"/approvals/{approval_id}",
    "sale": lambda sale_id: f"/sales/{sale_id}",
    "inventory_variant": lambda variant_id: f"/inventory?variant={variant_id}",
}


def action_path(kind: str, **params) -> str:
    """Build a safe relative path, or ``""`` if ``kind``/params are unknown."""

    builder = _BUILDERS.get(kind)
    if builder is None:
        return ""
    try:
        return builder(**params)
    except TypeError:
        return ""
