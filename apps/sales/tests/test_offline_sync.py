"""Stage 16 — offline batch synchronisation outcomes and guarantees."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import DeviceStatus, RegisteredDevice
from apps.accounts.tests.factories import EmployeeFactory, RegisteredDeviceFactory
from apps.catalog.services.pricing import set_active_price
from apps.core.exceptions import APIError
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    Payment,
    Sale,
    SaleSource,
    SaleStatus,
)
from apps.sales.services.offline import (
    OfflinePaymentInput,
    OfflineSaleInput,
    issue_offline_authorization,
    revoke_offline_authorization,
    sync_offline_batch,
)
from apps.sales.services.offline_snapshot import verify_authorization_token
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


def _device(branch):
    return RegisteredDevice.objects.filter(
        branch=branch, status=DeviceStatus.ACTIVE
    ).first() or RegisteredDeviceFactory(branch=branch)


def _session(branch, owner, variants):
    made = {}
    for sku, price, qty in variants:
        made[sku] = stocked_variant(
            branch, owner, price=price, quantity=qty, sku=sku, low_stock_level=2
        )
    cashier = EmployeeFactory(branch=branch)
    auth = issue_offline_authorization(
        branch=branch, device=_device(branch), cashier=cashier, owner=owner
    )
    snapshot = verify_authorization_token(auth.signed_token)["snapshot"]
    return auth, snapshot, cashier, made


def _sale(*, seq, items, payments, cid=None, created_at=None, name=""):
    return OfflineSaleInput(
        client_sale_id=cid or uuid.uuid4(),
        device_sequence=seq,
        offline_created_at=created_at or timezone.now(),
        items=items,
        payments=payments,
        customer_name=name,
    )


def _cash(amount):
    return [OfflinePaymentInput(method="CASH", amount=Decimal(amount))]


class TestAccept:
    def test_accepted_creates_official_offline_sale_and_receipt(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1500.00", 10)])
        when = timezone.now()
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 2}],
            payments=_cash("3000.00"),
            created_at=when,
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.ACCEPTED
        assert result.receipt_number

        sale = Sale.objects.get(client_sale_id=s.client_sale_id)
        assert sale.status == SaleStatus.COMPLETED
        assert sale.source == SaleSource.OFFLINE
        assert sale.total == Decimal("3000.00")
        assert abs(sale.completed_at - when) < timedelta(seconds=2)
        assert Payment.objects.filter(sale=sale, offline_confirmed=True).count() == 1
        assert (
            InventoryBalance.objects.get(branch=branch, variant=made["PNT-1"]).quantity
            == 8
        )

    def test_fixed_snapshot_price_survives_a_later_online_price_change(
        self, branch, owner
    ):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        set_active_price(
            variant=made["PNT-1"], amount=Decimal("4000.00"), changed_by=owner
        )
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 2}],
            payments=_cash("2000.00"),  # 2 x the FIXED 1000, not the new 4000
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.ACCEPTED
        assert Sale.objects.get(client_sale_id=s.client_sale_id).total == Decimal(
            "2000.00"
        )

    def test_split_cash_pos_transfer_payments(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 20)])
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 6}],
            payments=[
                OfflinePaymentInput("CASH", Decimal("2000.00"), Decimal("2000.00")),
                OfflinePaymentInput("POS", Decimal("2000.00"), reference="POS-77"),
                OfflinePaymentInput("TRANSFER", Decimal("2000.00"), reference="TRF-88"),
            ],
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.ACCEPTED
        assert (
            Payment.objects.filter(sale__client_sale_id=s.client_sale_id).count() == 3
        )


class TestReject:
    def _one(self, branch, owner, sale, qty_stock=10, price="1000.00"):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", price, qty_stock)])
        sale.items = [{"variant_id": str(made["PNT-1"].id), "quantity": sale.items}]
        sale.offline_created_at = timezone.now()
        return sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[sale]
        )[0]

    def test_payment_mismatch_rejected(self, branch, owner):
        result = self._one(
            branch, owner, _sale(seq=1, items=2, payments=_cash("1999.00"))
        )
        assert result.outcome == OfflineSyncOutcome.REJECTED
        assert result.detail_code == "payment_mismatch"

    def test_pos_without_reference_rejected(self, branch, owner):
        result = self._one(
            branch,
            owner,
            _sale(
                seq=1,
                items=2,
                payments=[OfflinePaymentInput("POS", Decimal("2000.00"))],
            ),
        )
        assert result.outcome == OfflineSyncOutcome.REJECTED
        assert result.detail_code == "reference_required"

    def test_fractional_quantity_rejected(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 1.5}],
            payments=_cash("1500.00"),
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.REJECTED
        assert result.detail_code == "invalid_quantity"

    def test_sale_dated_outside_the_authorization_window_rejected(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 1}],
            payments=_cash("1000.00"),
            created_at=auth.expires_at + timedelta(minutes=5),
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.REJECTED
        assert result.detail_code == "outside_window"

    def test_late_sync_of_an_in_window_sale_still_accepted(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        # authorization already expired, but the sale was made inside the window
        auth.issued_at = timezone.now() - timedelta(hours=30)
        auth.expires_at = timezone.now() - timedelta(hours=6)
        auth.save(update_fields=["issued_at", "expires_at", "updated_at"])
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 1}],
            payments=_cash("1000.00"),
            created_at=timezone.now() - timedelta(hours=10),
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.ACCEPTED


class TestOrderingAndDuplicates:
    def test_processed_in_device_sequence_order(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 20)])
        vid = str(made["PNT-1"].id)
        s2 = _sale(
            seq=2, items=[{"variant_id": vid, "quantity": 1}], payments=_cash("1000.00")
        )
        s1 = _sale(
            seq=1, items=[{"variant_id": vid, "quantity": 1}], payments=_cash("1000.00")
        )
        results = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s2, s1]
        )
        assert [r.device_sequence for r in results] == [1, 2]
        recs = list(
            OfflineSaleSyncRecord.objects.filter(authorization=auth).order_by(
                "device_sequence"
            )
        )
        assert [r.device_sequence for r in recs] == [1, 2]

    def test_duplicate_device_sequence_is_rejected(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 20)])
        vid = str(made["PNT-1"].id)
        a = _sale(
            seq=5, items=[{"variant_id": vid, "quantity": 1}], payments=_cash("1000.00")
        )
        b = _sale(
            seq=5, items=[{"variant_id": vid, "quantity": 1}], payments=_cash("1000.00")
        )
        results = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[a, b]
        )
        outcomes = sorted(r.outcome for r in results)
        assert outcomes == [OfflineSyncOutcome.ACCEPTED, OfflineSyncOutcome.REJECTED]
        rejected = next(r for r in results if r.outcome == OfflineSyncOutcome.REJECTED)
        assert rejected.detail_code == "duplicate_sequence"

    def test_batch_retry_is_idempotent(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 20)])
        vid = str(made["PNT-1"].id)
        s = _sale(
            seq=1, items=[{"variant_id": vid, "quantity": 2}], payments=_cash("2000.00")
        )
        first = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )[0]
        second = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )[0]
        assert first.outcome == OfflineSyncOutcome.ACCEPTED
        assert second.outcome == OfflineSyncOutcome.DUPLICATE
        assert second.sale_id == first.sale_id
        assert second.receipt_number == first.receipt_number
        assert Sale.objects.filter(client_sale_id=s.client_sale_id).count() == 1


class TestConflictAndReview:
    def test_stock_conflict_is_retained_never_negative_never_partial(
        self, branch, owner
    ):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 5)])
        vid = str(made["PNT-1"].id)
        good = _sale(
            seq=1, items=[{"variant_id": vid, "quantity": 3}], payments=_cash("3000.00")
        )
        oversell = _sale(
            seq=2, items=[{"variant_id": vid, "quantity": 3}], payments=_cash("3000.00")
        )
        results = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[good, oversell]
        )
        by_seq = {r.device_sequence: r for r in results}
        assert by_seq[1].outcome == OfflineSyncOutcome.ACCEPTED
        assert by_seq[2].outcome == OfflineSyncOutcome.CONFLICT
        assert by_seq[2].detail_code == "stock_not_available"

        # the conflicting sale left nothing behind
        assert not Sale.objects.filter(client_sale_id=oversell.client_sale_id).exists()
        assert not Payment.objects.filter(
            sale__client_sale_id=oversell.client_sale_id
        ).exists()
        balance = InventoryBalance.objects.get(branch=branch, variant=made["PNT-1"])
        assert balance.quantity == 2  # only the accepted sale applied
        assert (
            StockMovement.objects.filter(
                variant=made["PNT-1"], movement_type=MovementType.SALE
            ).count()
            == 1
        )
        record = OfflineSaleSyncRecord.objects.get(
            client_sale_id=oversell.client_sale_id
        )
        assert record.resolved is False
        assert record.redacted_payload["items"]  # kept for owner review

    def test_revoked_authorization_routes_everything_to_owner_review(
        self, branch, owner
    ):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        revoke_offline_authorization(authorization=auth, owner=owner, reason="lost")
        auth.refresh_from_db()
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 1}],
            payments=_cash("1000.00"),
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.OWNER_REVIEW_REQUIRED
        assert not Sale.objects.filter(client_sale_id=s.client_sale_id).exists()
        assert (
            OfflineSaleSyncRecord.objects.get(client_sale_id=s.client_sale_id).outcome
            == OfflineSyncOutcome.OWNER_REVIEW_REQUIRED
        )


class TestDownstream:
    def test_accepted_offline_sales_feed_reports_and_inventory(self, branch, owner):
        from apps.finance.services.reports import profit_report

        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 10)])
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 4}],
            payments=_cash("4000.00"),
        )
        sync_offline_batch(branch=branch, authorization=auth, snapshot=snap, sales=[s])
        report = profit_report(branch=branch, period="month")
        assert report["revenue"] == Decimal("4000.00")
        assert (
            InventoryBalance.objects.get(branch=branch, variant=made["PNT-1"]).quantity
            == 6
        )

    @override_settings(OFFLINE_SYNC_MAX_BATCH=2)
    def test_batch_size_limit(self, branch, owner):
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 50)])
        vid = str(made["PNT-1"].id)
        sales = [
            _sale(
                seq=i,
                items=[{"variant_id": vid, "quantity": 1}],
                payments=_cash("1000.00"),
            )
            for i in range(3)
        ]
        with pytest.raises(APIError) as err:
            sync_offline_batch(
                branch=branch, authorization=auth, snapshot=snap, sales=sales
            )
        assert err.value.code == "batch_too_large"

    def test_conflicting_stock_from_a_concurrent_online_op_is_a_conflict(
        self, branch, owner
    ):
        # Simulate stock that dropped below the snapshot after issue (e.g. a
        # write that slipped in): the offline sale must become a CONFLICT, not
        # invent stock and not go negative.
        auth, snap, _c, made = _session(branch, owner, [("PNT-1", "1000.00", 5)])
        balance = lock_balances(branch, [made["PNT-1"].id])[made["PNT-1"].id]
        write_movement(
            balance=balance,
            delta=-4,  # live stock now 1
            movement_type=MovementType.ADJUSTMENT,
            reference_type="adjustment",
            reference_id=uuid.uuid4(),
        )
        s = _sale(
            seq=1,
            items=[{"variant_id": str(made["PNT-1"].id), "quantity": 3}],
            payments=_cash("3000.00"),
        )
        [result] = sync_offline_batch(
            branch=branch, authorization=auth, snapshot=snap, sales=[s]
        )
        assert result.outcome == OfflineSyncOutcome.CONFLICT
        assert (
            InventoryBalance.objects.get(branch=branch, variant=made["PNT-1"]).quantity
            == 1
        )
