"""Stage 16 — offline checkout: secret / protected-field hygiene and rollback."""

import logging
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator

from apps.accounts.tests.factories import EmployeeFactory, RegisteredDeviceFactory
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import OfflineSyncOutcome, Payment, Sale
from apps.sales.services.offline import (
    OfflinePaymentInput,
    OfflineSaleInput,
    issue_offline_authorization,
    sync_offline_batch,
)
from apps.sales.services.offline_snapshot import verify_authorization_token
from apps.sales.tests.factories import stocked_variant

ROOT = Path(settings.BASE_DIR)
pytestmark = pytest.mark.django_db


def _session(branch, owner, *, qty=10, price="1000.00"):
    variant = stocked_variant(
        branch, owner, price=price, quantity=qty, sku="SEC-1", low_stock_level=1
    )
    auth = issue_offline_authorization(
        branch=branch,
        device=RegisteredDeviceFactory(branch=branch),
        cashier=EmployeeFactory(branch=branch),
        owner=owner,
    )
    snap = verify_authorization_token(auth.signed_token)["snapshot"]
    return variant, auth, snap


def _sale(variant, *, seq=1, qty=2, phone="", reference="", cid=None):
    payment = OfflinePaymentInput(
        method="POS" if reference else "CASH",
        amount=Decimal(str(1000 * qty)),
        reference=reference,
    )
    return OfflineSaleInput(
        client_sale_id=cid or uuid.uuid4(),
        device_sequence=seq,
        offline_created_at=timezone.now(),
        items=[{"variant_id": str(variant.id), "quantity": qty}],
        payments=[payment],
        customer_phone=phone,
    )


class TestNoSecretInSchema:
    def test_signing_key_never_appears_in_the_openapi_schema(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        blob = str(schema)
        assert settings.OFFLINE_SIGNING_KEY not in blob
        assert "OFFLINE_SIGNING_KEY" not in blob

    def test_sync_record_schema_has_no_signed_token(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        for name, component in schema["components"]["schemas"].items():
            if "OfflineSaleSyncRecord" in name or "OfflineSyncRecord" in name:
                assert "signed_token" not in component.get("properties", {})

    def test_committed_openapi_yaml_has_no_signing_secret(self):
        text = (ROOT / "openapi.yml").read_text(encoding="utf-8")
        assert settings.OFFLINE_SIGNING_KEY not in text
        assert "OFFLINE_SIGNING_KEY" not in text


class TestNoSecretInLogs:
    def test_sync_does_not_log_token_phone_or_payment_reference(
        self, branch, owner, caplog
    ):
        variant, auth, snap = _session(branch, owner)
        sale = _sale(variant, phone="08055555555", reference="TRANSFER-REF-XYZ", qty=2)
        with caplog.at_level(logging.DEBUG):
            sync_offline_batch(
                branch=branch, authorization=auth, snapshot=snap, sales=[sale]
            )
        blob = caplog.text
        assert auth.signed_token[:40] not in blob
        assert "08055555555" not in blob
        assert "TRANSFER-REF-XYZ" not in blob

    def test_retained_record_payload_excludes_phone_and_reference(self, branch, owner):
        variant, auth, snap = _session(branch, owner, qty=3)
        good = _sale(variant, seq=1, qty=2)
        bad = _sale(variant, seq=2, qty=2, phone="08077777777", reference="REF-SECRET")
        results = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[good, bad]
        )
        conflict = next(r for r in results if r.outcome == OfflineSyncOutcome.CONFLICT)
        from apps.sales.models import OfflineSaleSyncRecord

        record = OfflineSaleSyncRecord.objects.get(
            client_sale_id=conflict.client_sale_id
        )
        blob = str(record.redacted_payload)
        assert "08077777777" not in blob
        assert "REF-SECRET" not in blob
        assert record.redacted_payload["payments"][0]["has_reference"] is True


class TestRollback:
    def test_a_rejected_sale_leaves_no_sale_payment_or_movement(self, branch, owner):
        variant, auth, snap = _session(branch, owner, qty=10)
        bad = _sale(variant, seq=1, qty=2)
        bad.payments = [OfflinePaymentInput(method="CASH", amount=Decimal("1.00"))]
        start_balance = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[bad]
        )
        assert result.outcome == OfflineSyncOutcome.REJECTED
        assert not Sale.objects.filter(client_sale_id=bad.client_sale_id).exists()
        assert not Payment.objects.filter(
            sale__client_sale_id=bad.client_sale_id
        ).exists()
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == start_balance
        )
        assert not StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.SALE
        ).exists()

    def test_one_bad_sale_does_not_corrupt_the_valid_ones_in_the_batch(
        self, branch, owner
    ):
        variant, auth, snap = _session(branch, owner, qty=10)
        good_a = _sale(variant, seq=1, qty=1)
        bad = _sale(variant, seq=2, qty=1)
        bad.payments = [OfflinePaymentInput(method="CASH", amount=Decimal("999.00"))]
        good_b = _sale(variant, seq=3, qty=1)
        results = sync_offline_batch(
            branch=branch,
            authorization=auth,
            snapshot=snap,
            sales=[good_a, bad, good_b],
        )
        by_seq = {r.device_sequence: r.outcome for r in results}
        assert by_seq[1] == OfflineSyncOutcome.ACCEPTED
        assert by_seq[2] == OfflineSyncOutcome.REJECTED
        assert by_seq[3] == OfflineSyncOutcome.ACCEPTED
        assert Sale.objects.filter(source="OFFLINE", branch=branch).count() == 2
