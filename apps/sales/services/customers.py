"""Customer helpers. Name and phone are both optional; a walk-in has neither."""

from __future__ import annotations

import re

from apps.sales.models import Customer

_KEEP = re.compile(r"[^\d+]")


def normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = _KEEP.sub("", raw.strip())
    # collapse a leading "+" that isn't at position 0
    if "+" in cleaned[1:]:
        cleaned = cleaned[0] + cleaned[1:].replace("+", "")
    return cleaned


def mask_phone(phone: str | None) -> str:
    phone = phone or ""
    if len(phone) <= 4:
        return "*" * len(phone)
    return "*" * (len(phone) - 4) + phone[-4:]


def resolve_customer(branch, data: dict | None) -> Customer | None:
    """Return a Customer for the given ``{name, phone}`` dict, or None for walk-in.

    Never creates a row when both fields are blank.
    """

    if not data:
        return None
    name = (data.get("name") or "").strip()
    phone = normalize_phone(data.get("phone"))
    if not name and not phone:
        return None
    if phone:
        existing = Customer.objects.filter(branch=branch, phone=phone).first()
        if existing:
            if name and existing.name != name:
                existing.name = name
                existing.save(update_fields=["name", "updated_at"])
            return existing
    return Customer.objects.create(branch=branch, name=name, phone=phone)
