"""The versioned, signed fixed-catalogue snapshot for offline checkout.

The server issues one snapshot per offline authorization: active variants with
their names, SKUs, fixed selling prices, available quantities and low-stock
thresholds — and nothing else. It never carries cost prices, profit, audit data,
customer information, user credentials or the signing secret.

Signing and verification go through :mod:`django.core.signing`, which builds an
HMAC-SHA256 signature and checks it with ``constant_time_compare``. Any edit to
the signed package — payload or signature — fails verification.
"""

from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.catalog.selectors import current_price
from apps.core.money import to_money
from apps.inventory.models import InventoryBalance

_SALT = "viable-stone.offline.authorization.v1"


class SnapshotVerificationError(Exception):
    """Raised when a signed authorization package fails verification."""


def _signing_key() -> str:
    # A dedicated server-side secret from the environment; falls back to the
    # Django SECRET_KEY (also env-only) when not separately configured.
    return getattr(settings, "OFFLINE_SIGNING_KEY", "") or settings.SECRET_KEY


def build_catalogue_snapshot(*, branch, version: int) -> dict:
    """Fixed catalogue for ``branch``: active, priced variants only."""

    balances = {b.variant_id: b for b in InventoryBalance.objects.filter(branch=branch)}
    variants = (
        ProductVariant.objects.select_related("product")
        .filter(product__branch=branch, is_active=True, product__is_active=True)
        .order_by("sku")
    )
    rows = []
    for variant in variants:
        price = current_price(variant)
        if price is None:
            continue
        balance = balances.get(variant.id)
        rows.append(
            {
                "variant_id": str(variant.id),
                "name": variant.product.name,
                "sku": variant.sku,
                "price": str(to_money(price.amount)),
                "quantity": int(balance.quantity) if balance else 0,
                "low_stock_level": int(variant.low_stock_level),
            }
        )
    return {
        "version": int(version),
        "branch_id": str(branch.id),
        "generated_at": timezone.now().isoformat(),
        "variants": rows,
    }


def sign_authorization_package(
    *,
    snapshot: dict,
    branch_id,
    device_id,
    authorization_id,
    cashier_id,
    issued_at,
    expires_at,
) -> str:
    """Return a signed, compressed token binding the snapshot to one device."""

    payload = {
        "authorization_id": str(authorization_id),
        "branch_id": str(branch_id),
        "device_id": str(device_id),
        "cashier_id": str(cashier_id),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "snapshot": snapshot,
    }
    return signing.dumps(payload, key=_signing_key(), salt=_SALT, compress=True)


def verify_authorization_token(token: str) -> dict:
    """Constant-time verify a signed package. Raise on any tampering."""

    try:
        return signing.loads(token, key=_signing_key(), salt=_SALT)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise SnapshotVerificationError("invalid_signature") from exc
