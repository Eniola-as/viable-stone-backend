"""Stage 16 — signed fixed-catalogue snapshot: build, sign, tamper-detection."""

import pytest
from django.test import override_settings

from apps.catalog.services.pricing import set_active_price
from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.inventory.services.stock import open_stock
from apps.sales.services.offline_snapshot import (
    SnapshotVerificationError,
    build_catalogue_snapshot,
    sign_authorization_package,
    verify_authorization_token,
)

pytestmark = pytest.mark.django_db

_FORBIDDEN_KEYS = {
    "cost",
    "unit_cost",
    "average_unit_cost",
    "phone",
    "email",
    "password",
    "secret",
    "token",
    "mfa",
    "customer",
}


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _make_variant(branch, owner, *, sku, price, qty, low=5, active=True):
    variant = ProductVariantFactory(
        product=ProductFactory(branch=branch, is_active=True),
        sku=sku,
        is_active=active,
        low_stock_level=low,
    )
    set_active_price(variant=variant, amount=price, changed_by=owner)
    if qty:
        open_stock(
            branch=branch,
            variant=variant,
            quantity=qty,
            unit_cost="100.00",
            created_by=owner,
        )
    return variant


class TestBuildSnapshot:
    def test_contains_active_priced_variants_with_fixed_price_and_qty(
        self, branch, owner
    ):
        from decimal import Decimal

        v1 = _make_variant(branch, owner, sku="AAA-1", price=Decimal("1500.00"), qty=12)
        _make_variant(branch, owner, sku="BBB-2", price=Decimal("500.00"), qty=0)
        # inactive variant is excluded
        _make_variant(
            branch, owner, sku="CCC-3", price=Decimal("900.00"), qty=3, active=False
        )

        snap = build_catalogue_snapshot(branch=branch, version=1)
        assert snap["version"] == 1
        assert snap["branch_id"] == str(branch.id)
        skus = {row["sku"] for row in snap["variants"]}
        assert skus == {"AAA-1", "BBB-2"}
        row = next(r for r in snap["variants"] if r["sku"] == "AAA-1")
        assert row["variant_id"] == str(v1.id)
        assert row["price"] == "1500.00"
        assert row["quantity"] == 12
        assert row["low_stock_level"] == 5

    def test_snapshot_carries_no_cost_secret_or_customer_fields(self, branch, owner):
        from decimal import Decimal

        _make_variant(branch, owner, sku="AAA-1", price=Decimal("1500.00"), qty=12)
        snap = build_catalogue_snapshot(branch=branch, version=3)
        keys = {k.lower() for k in _walk_keys(snap)}
        assert keys.isdisjoint(_FORBIDDEN_KEYS)


class TestSignAndVerify:
    def _package(self, branch, **over):
        import uuid
        from datetime import timedelta

        from django.utils import timezone

        issued = timezone.now()
        params = {
            "snapshot": {"version": 1, "branch_id": str(branch.id), "variants": []},
            "branch_id": str(branch.id),
            "device_id": str(uuid.uuid4()),
            "authorization_id": str(uuid.uuid4()),
            "cashier_id": str(uuid.uuid4()),
            "issued_at": issued,
            "expires_at": issued + timedelta(hours=24),
        }
        params.update(over)
        return params

    def test_round_trip_recovers_the_binding_and_snapshot(self, branch):
        params = self._package(branch)
        token = sign_authorization_package(**params)
        decoded = verify_authorization_token(token)
        assert decoded["branch_id"] == params["branch_id"]
        assert decoded["device_id"] == params["device_id"]
        assert decoded["authorization_id"] == params["authorization_id"]
        assert decoded["snapshot"]["branch_id"] == str(branch.id)

    def test_tampered_token_is_rejected(self, branch):
        token = sign_authorization_package(**self._package(branch))
        tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
        with pytest.raises(SnapshotVerificationError):
            verify_authorization_token(tampered)

    def test_body_edit_breaks_the_signature(self, branch):
        token = sign_authorization_package(**self._package(branch))
        head, _, tail = token.partition(":")
        # corrupt the payload segment, keep the rest
        with pytest.raises(SnapshotVerificationError):
            verify_authorization_token(head[:-2] + "zz:" + tail)

    def test_signature_is_key_scoped(self, branch):
        with override_settings(OFFLINE_SIGNING_KEY="key-one"):
            token = sign_authorization_package(**self._package(branch))
        with override_settings(OFFLINE_SIGNING_KEY="key-two"):
            with pytest.raises(SnapshotVerificationError):
                verify_authorization_token(token)
