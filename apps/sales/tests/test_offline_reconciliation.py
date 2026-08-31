"""Safe reconciliation of accounting-impacting offline sync records.

Replaces the unsafe "mark resolved with a note" for CONFLICT / REJECTED /
OWNER_REVIEW_REQUIRED. New endpoint:

    POST /api/v1/offline/sync-records/{id}/reconcile/   (owner + MFA)

polymorphic on ``kind`` — RECORDED_AS_SALE | REFUNDED_AND_RETURNED |
LINKED_EXISTING_SALE. The backend re-reads the retained payload + verifies the
signed snapshot and creates the official Sale itself; a bare note can no longer
close these records.
"""

import uuid
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import OfflineDeviceAuthorization
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.catalog.services.pricing import set_active_price
from apps.core.models import AuditLog
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.management.commands.list_unsafe_offline_resolutions import (
    unsafe_offline_resolutions,
)
from apps.sales.models import (
    OfflineSaleReconciliation,
    OfflineSaleSyncRecord,
    Payment,
    Sale,
    SaleItem,
)
from apps.sales.services.offline import revoke_offline_authorization
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline"


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _session(login_as, branch, owner, *, price="1000.00", qty=20, cost="600.00"):
    variant = stocked_variant(branch, owner, price=price, quantity=qty, unit_cost=cost)
    cashier = EmployeeFactory(branch=branch)
    oc = login_as(owner)
    device_id = oc.post(f"{BASE}/devices/", {"name": "Till"}, format="json").json()[
        "id"
    ]
    auth = oc.post(
        f"{BASE}/authorizations/",
        {"device": device_id, "cashier": str(cashier.id)},
        format="json",
    ).json()
    return variant, cashier, auth


def _sale_payload(variant, *, seq, qty, amount, cid=None, method="CASH", reference=""):
    pay = {"method": method, "amount": amount}
    if reference:
        pay["reference"] = reference
    return {
        "client_sale_id": str(cid or uuid.uuid4()),
        "device_sequence": seq,
        "offline_created_at": timezone.now().isoformat(),
        "items": [{"variant_id": str(variant.id), "quantity": qty}],
        "payments": [pay],
    }


def _sync(client, auth, sales):
    return client.post(
        f"{BASE}/sync/",
        {"authorization_token": auth["signed_token"], "sales": sales},
        format="json",
    ).json()["results"]


def _record(cid):
    return OfflineSaleSyncRecord.objects.get(client_sale_id=cid)


def _reconcile(owner_client, record_id, body):
    return owner_client.post(
        f"{BASE}/sync-records/{record_id}/reconcile/", body, format="json"
    )


def _conflict_record(login_as, branch, owner, *, stock=5, sale_qty=3):
    """Sync two sales that together oversell -> 2nd is CONFLICT stock_not_available."""
    variant, cashier, auth = _session(login_as, branch, owner, qty=stock)
    cc = login_as(cashier)
    good, bad = uuid.uuid4(), uuid.uuid4()
    _sync(
        cc,
        auth,
        [
            _sale_payload(
                variant, seq=1, qty=sale_qty, amount=str(1000 * sale_qty), cid=good
            ),
            _sale_payload(
                variant, seq=2, qty=sale_qty, amount=str(1000 * sale_qty), cid=bad
            ),
        ],
    )
    rec = _record(bad)
    assert rec.outcome == "CONFLICT" and rec.detail_code == "stock_not_available"
    return variant, cashier, auth, rec


def _rejected_record(login_as, branch, owner, *, detail="payment_mismatch"):
    variant, cashier, auth = _session(login_as, branch, owner, qty=20)
    cc = login_as(cashier)
    cid = uuid.uuid4()
    if detail == "payment_mismatch":
        payload = _sale_payload(variant, seq=1, qty=2, amount="1999.00", cid=cid)
    else:
        raise AssertionError(detail)
    _sync(cc, auth, [payload])
    rec = _record(cid)
    assert rec.outcome == "REJECTED" and rec.detail_code == detail
    return variant, cashier, auth, rec


def _owner_review_record(login_as, branch, owner):
    variant, cashier, auth = _session(login_as, branch, owner, qty=20)
    auth_obj = OfflineDeviceAuthorization.objects.get(id=auth["id"])
    revoke_offline_authorization(authorization=auth_obj, owner=owner, reason="lost")
    cc = login_as(cashier)
    cid = uuid.uuid4()
    _sync(cc, auth, [_sale_payload(variant, seq=1, qty=2, amount="2000.00", cid=cid)])
    rec = _record(cid)
    assert rec.outcome == "OWNER_REVIEW_REQUIRED"
    return variant, cashier, auth, rec


def _owner_review_multi(login_as, branch, owner, *, payments, qty, price="1000.00"):
    """An OWNER_REVIEW_REQUIRED record whose single offline sale carries the
    given (multi-line) payments. Revoking the session forces OWNER_REVIEW
    before any payment validation, so the retained payload is stored verbatim.
    """

    variant, cashier, auth = _session(
        login_as, branch, owner, price=price, qty=qty + 20
    )
    auth_obj = OfflineDeviceAuthorization.objects.get(id=auth["id"])
    revoke_offline_authorization(authorization=auth_obj, owner=owner, reason="lost")
    cid = uuid.uuid4()
    _sync(
        login_as(cashier),
        auth,
        [
            {
                "client_sale_id": str(cid),
                "device_sequence": 1,
                "offline_created_at": timezone.now().isoformat(),
                "items": [{"variant_id": str(variant.id), "quantity": qty}],
                "payments": payments,
            }
        ],
    )
    rec = _record(cid)
    assert rec.outcome == "OWNER_REVIEW_REQUIRED"
    return variant, cashier, auth, rec


def _tampered_owner_review(login_as, branch, owner):
    """An OWNER_REVIEW_REQUIRED record whose retained signed token no longer
    verifies (binding broken) — the redacted payload is still intact.
    """

    variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
    rec.authorization.signed_token = rec.authorization.signed_token + "x"
    rec.authorization.save(update_fields=["signed_token"])
    return variant, rec


def _completed_sale(
    login_as, branch, *, variant, quantity, amount, method="CASH", reference=""
):
    """Create a normal online COMPLETED sale in ``branch`` (no offline device)."""

    pay = {"method": method, "amount": amount}
    if reference:
        pay["reference"] = reference
    return (
        login_as(EmployeeFactory(branch=branch))
        .post(
            "/api/v1/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": quantity}],
                "payments": [pay],
            },
            format="json",
        )
        .json()
    )


# --------------------------------------------------------------------------- #
# resolution safety — note alone cannot close an accounting-impacting record  #
# --------------------------------------------------------------------------- #


class TestNoteOnlyBlocked:
    @pytest.mark.parametrize("maker", ["conflict", "rejected", "owner_review"])
    def test_bare_note_resolve_is_rejected_and_record_stays_unresolved(
        self, login_as, branch, owner, maker
    ):
        makers = {
            "conflict": _conflict_record,
            "rejected": _rejected_record,
            "owner_review": _owner_review_record,
        }
        _v, _c, _a, rec = makers[maker](login_as, branch, owner)
        res = login_as(owner).post(
            f"{BASE}/sync-records/{rec.id}/resolve/",
            {"note": "counted stock and moved on"},
            format="json",
        )
        assert res.status_code == 409
        assert res.json()["code"] == "offline_reconciliation_required"
        rec.refresh_from_db()
        assert rec.resolved is False
        status = (
            login_as(owner)
            .get(f"{BASE}/authorizations/{rec.authorization_id}/status/")
            .json()
        )
        assert status["pending_review_count"] >= 1

    def test_historical_resolved_record_is_untouched(self, login_as, branch, owner):
        _v, _c, _a, rec = _conflict_record(login_as, branch, owner)
        # simulate a pre-existing resolved row (old note-only path)
        rec.resolved = True
        rec.resolved_by = owner
        rec.resolved_at = timezone.now()
        rec.resolution_note = "historical"
        rec.save()
        got = login_as(owner).get(f"{BASE}/sync-records/{rec.id}/").json()
        assert got["resolved"] is True


# --------------------------------------------------------------------------- #
# RECORDED_AS_SALE — accounting                                               #
# --------------------------------------------------------------------------- #


class TestRecordedAsSaleAccounting:
    def test_owner_review_record_becomes_a_correct_official_sale(
        self, login_as, branch, owner
    ):
        variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        before_bal = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "customer kept the paint, cash is in the drawer",
            },
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["reconciliation"]["kind"] == "RECORDED_AS_SALE"
        assert body["sync_record"]["resolved"] is True

        sale = Sale.objects.get(client_sale_id=rec.client_sale_id)
        assert sale.status == "COMPLETED"
        assert sale.source == "OFFLINE"
        assert sale.subtotal == Decimal("2000.00")
        assert sale.total == Decimal("2000.00")
        assert sale.receipt_number
        assert body["reconciliation"]["receipt_number"] == sale.receipt_number
        # payments recorded once, exactly covering the total
        pays = Payment.objects.filter(sale=sale)
        assert pays.count() == 1
        assert pays.first().amount == Decimal("2000.00")
        assert pays.first().offline_confirmed is True
        # COGS from server-authoritative average cost (600), not device data
        item = SaleItem.objects.get(sale=sale)
        assert item.unit_cost_snapshot == Decimal("600.00")
        assert item.quantity == 2
        # stock deducted exactly once
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == before_bal - 2
        )
        assert (
            StockMovement.objects.filter(
                variant=variant,
                movement_type=MovementType.SALE,
                reference_id=sale.id,
            ).count()
            == 1
        )

    def test_revenue_and_profit_increase_exactly_once(self, login_as, branch, owner):
        from apps.finance.services.reports import profit_report

        _variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept goods, kept payment"},
        )
        report = profit_report(branch=branch, period="month")
        assert report["revenue"] == Decimal("2000.00")
        assert report["cogs"] == Decimal("1200.00")  # 2 x 600
        assert report["gross_profit"] == Decimal("800.00")

        # retry is idempotent -> revenue still 2000, one receipt
        again = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept goods, kept payment"},
        )
        assert again.status_code == 200
        assert profit_report(branch=branch, period="month")["revenue"] == Decimal(
            "2000.00"
        )
        assert Sale.objects.filter(client_sale_id=rec.client_sale_id).count() == 1

    def test_official_receipt_created_once(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        r1 = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept and paid"},
        )
        r2 = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept and paid"},
        )
        assert (
            r1.json()["reconciliation"]["receipt_number"]
            == (r2.json()["reconciliation"]["receipt_number"])
        )

    def test_offline_completion_time_preserved_when_in_window(
        self, login_as, branch, owner
    ):
        # OWNER_REVIEW_REQUIRED: the sale's offline_created_at is well inside the
        # authorised window, so the official Sale keeps that time.
        _variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "customer kept the goods, cash confirmed",
            },
        )
        assert res.status_code == 200, res.content
        sale = Sale.objects.get(client_sale_id=rec.client_sale_id)
        assert abs((sale.completed_at - rec.offline_created_at).total_seconds()) < 2
        assert res.json()["reconciliation"]["completion_time_substituted"] is False

    def test_transfer_payment_requires_owner_supplied_reference(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = _session(login_as, branch, owner, qty=20)
        cc = login_as(cashier)
        keep, dup = uuid.uuid4(), uuid.uuid4()
        # two sales share device_sequence 1 -> the 2nd is REJECTED
        # duplicate_sequence, with a valid POS payment (amount matches the total)
        _sync(
            cc,
            auth,
            [
                _sale_payload(
                    variant,
                    seq=1,
                    qty=2,
                    amount="2000.00",
                    cid=keep,
                    method="POS",
                    reference="POS-A",
                ),
                _sale_payload(
                    variant,
                    seq=1,
                    qty=2,
                    amount="2000.00",
                    cid=dup,
                    method="POS",
                    reference="POS-B",
                ),
            ],
        )
        rec = _record(dup)
        assert rec.outcome == "REJECTED" and rec.detail_code == "duplicate_sequence"
        no_ref = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "customer kept the goods"},
        )
        assert no_ref.status_code == 400
        assert no_ref.json()["code"] == "reference_required"

        # the owner sees the retained payment index/method/amount to key on
        detail = login_as(owner).get(f"{BASE}/sync-records/{rec.id}/").json()
        rp = detail["retained_payments"]
        assert rp == [
            {
                "payment_index": 0,
                "method": "POS",
                "amount": "2000.00",
                "reference_required": True,
            }
        ]

        ok = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "customer kept the goods",
                "payment_references": [
                    {"payment_index": 0, "reference": "POS-SLIP-88"}
                ],
            },
        )
        assert ok.status_code == 200, ok.content
        pay = Payment.objects.get(sale__client_sale_id=dup)
        assert pay.method == "POS" and pay.reference == "POS-SLIP-88"


# --------------------------------------------------------------------------- #
# RECORDED_AS_SALE — stock-conflict physical count                            #
# --------------------------------------------------------------------------- #


class TestStockConflictReconciliation:
    def test_conflict_requires_a_physical_count(self, login_as, branch, owner):
        _v, _c, _a, rec = _conflict_record(login_as, branch, owner, stock=5, sale_qty=3)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "goods left"},
        )
        assert res.status_code == 409
        assert res.json()["code"] == "physical_count_required"
        rec.refresh_from_db()
        assert rec.resolved is False

    def test_missing_variant_count_fails(self, login_as, branch, owner):
        _v, _c, _a, rec = _conflict_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "goods left", "counts": []},
        )
        assert res.status_code == 400
        assert res.json()["code"] in {"count_variant_missing", "invalid_count"}

    def test_duplicate_variant_count_fails(self, login_as, branch, owner):
        variant, _c, _a, rec = _conflict_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "goods left",
                "counts": [
                    {"variant": str(variant.id), "counted_on_hand": 1},
                    {"variant": str(variant.id), "counted_on_hand": 2},
                ],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "count_variant_duplicated"

    def test_negative_count_fails(self, login_as, branch, owner):
        variant, _c, _a, rec = _conflict_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "goods left",
                "counts": [{"variant": str(variant.id), "counted_on_hand": -1}],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "invalid_count"

    def test_unexpected_variant_count_fails(self, login_as, branch, owner):
        variant, _c, _a, rec = _conflict_record(login_as, branch, owner)
        other = stocked_variant(branch, owner, price="500.00", quantity=3)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "goods left",
                "counts": [
                    {"variant": str(variant.id), "counted_on_hand": 2},
                    {"variant": str(other.id), "counted_on_hand": 1},
                ],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "count_variant_unexpected"

    def test_reconstruction_leaves_final_stock_equal_to_the_count(
        self, login_as, branch, owner
    ):
        # stock 5, first sale of 3 accepted -> db balance 2; conflict sale is 3.
        variant, _c, _a, rec = _conflict_record(
            login_as, branch, owner, stock=5, sale_qty=3
        )
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 2
        )
        # owner physically counts 1 on the shelf now
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "counted the shelf after the offline goods left",
                "counts": [{"variant": str(variant.id), "counted_on_hand": 1}],
            },
        )
        assert res.status_code == 200, res.content
        bal = InventoryBalance.objects.get(branch=branch, variant=variant)
        assert bal.quantity == 1  # == counted_on_hand
        assert bal.quantity >= 0
        # reconstruction correction: pre_sale = 1 + 3 = 4; db_before = 2; delta +2
        recon = OfflineSaleReconciliation.objects.get(sync_record=rec)
        count = recon.counts.get(variant=variant)
        assert count.correction_delta == 2
        assert count.db_quantity_before == 2
        adj = StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.OFFLINE_RECONCILIATION
        )
        assert adj.count() == 1
        assert adj.first().quantity_delta == 2
        assert "OFFLINE_RECONCILIATION" in adj.first().reason
        # the reconciled sale deducted stock exactly once (its own SALE movement)
        assert (
            StockMovement.objects.filter(
                variant=variant,
                movement_type=MovementType.SALE,
                reference_id=recon.sale_id,
            ).count()
            == 1
        )

    def test_no_supplier_restock_and_cost_unchanged(self, login_as, branch, owner):
        variant, _c, _a, rec = _conflict_record(
            login_as, branch, owner, stock=5, sale_qty=3
        )
        cost_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).average_unit_cost
        _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "counted the shelf now",
                "counts": [{"variant": str(variant.id), "counted_on_hand": 1}],
            },
        )
        bal = InventoryBalance.objects.get(branch=branch, variant=variant)
        assert bal.average_unit_cost == cost_before  # correction never moves cost
        assert not StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.RESTOCK
        ).exists()
        item = SaleItem.objects.get(sale__client_sale_id=rec.client_sale_id)
        assert item.unit_cost_snapshot == cost_before

    def test_count_higher_than_db_balance_still_lands_on_count(
        self, login_as, branch, owner
    ):
        # db balance 2 after the accepted sale; owner counts 4 (found extra stock)
        variant, _c, _a, rec = _conflict_record(
            login_as, branch, owner, stock=5, sale_qty=3
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "found extra on the shelf",
                "counts": [{"variant": str(variant.id), "counted_on_hand": 4}],
            },
        )
        assert res.status_code == 200, res.content
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 4
        )


# --------------------------------------------------------------------------- #
# REFUNDED_AND_RETURNED                                                       #
# --------------------------------------------------------------------------- #


class TestRefundedAndReturned:
    def test_full_refund_creates_no_sale_stock_or_revenue(
        self, login_as, branch, owner
    ):
        from apps.finance.services.reports import profit_report

        variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        bal_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        mv_before = StockMovement.objects.filter(variant=variant).count()
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "customer brought everything back, refunded in full",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert res.status_code == 200, res.content
        assert res.json()["reconciliation"]["kind"] == "REFUNDED_AND_RETURNED"
        assert res.json()["reconciliation"]["sale_id"] is None
        assert res.json()["reconciliation"]["refund_total"] == "2000.00"
        # retained payments agreed with the verified snapshot -> trusted figure
        assert (
            res.json()["reconciliation"]["amount_source"]
            == "RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT"
        )

        # rule 4: no Sale, revenue, COGS, receipt or stock movement
        assert not Sale.objects.filter(client_sale_id=rec.client_sale_id).exists()
        assert not Payment.objects.filter(
            sale__client_sale_id=rec.client_sale_id
        ).exists()
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == bal_before
        )
        assert StockMovement.objects.filter(variant=variant).count() == mv_before
        assert profit_report(branch=branch, period="month")["revenue"] == Decimal(
            "0.00"
        )
        recon = OfflineSaleReconciliation.objects.get(sync_record=rec)
        assert recon.refunds.count() == 1
        assert recon.refunds.first().amount == Decimal("2000.00")

    def test_refund_evidence_must_total_the_full_offline_amount(
        self, login_as, branch, owner
    ):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "partial only",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "1500.00"}],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "refund_total_mismatch"
        rec.refresh_from_db()
        assert rec.resolved is False

    def test_transfer_refund_requires_a_reference(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "sent it back by transfer",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "TRANSFER", "amount": "2000.00"}],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "reference_required"

    def test_not_fully_returned_is_directed_to_sale_plus_return(
        self, login_as, branch, owner
    ):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "only one item back",
                "all_goods_returned": False,
                "full_amount_refunded": False,
                "refunds": [{"method": "CASH", "amount": "1000.00"}],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "partial_reconciliation_unsupported"


# --------------------------------------------------------------------------- #
# LINKED_EXISTING_SALE (fallback)                                             #
# --------------------------------------------------------------------------- #


class TestLinkExistingSale:
    def _tampered_owner_review(self, login_as, branch, owner):
        return _tampered_owner_review(login_as, branch, owner)

    def test_untrusted_payload_cannot_auto_create_a_sale(self, login_as, branch, owner):
        _v, rec = self._tampered_owner_review(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept goods"},
        )
        assert res.status_code in (400, 409)
        assert res.json()["code"] == "offline_data_untrusted"
        rec.refresh_from_db()
        assert rec.resolved is False

    def test_owner_links_a_manually_entered_sale(self, login_as, branch, owner):
        variant, rec = self._tampered_owner_review(login_as, branch, owner)
        cashier = EmployeeFactory(branch=branch)
        sale = (
            login_as(cashier)
            .post(
                "/api/v1/sales/",
                {
                    "client_sale_id": str(uuid.uuid4()),
                    "items": [{"variant": str(variant.id), "quantity": 2}],
                    "payments": [{"method": "CASH", "amount": "2000.00"}],
                },
                format="json",
            )
            .json()
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "re-entered the sale from the paper till roll",
                "receipt_number": sale["receipt_number"],
            },
        )
        assert res.status_code == 200, res.content
        assert res.json()["reconciliation"]["linked_manually"] is True
        assert res.json()["reconciliation"]["sale_id"] == sale["id"]
        rec.refresh_from_db()
        assert rec.resolved is True
        assert str(rec.sale_id) == sale["id"]

    def test_cannot_link_a_sale_from_another_branch(self, login_as, branch, owner):
        _v, rec = self._tampered_owner_review(login_as, branch, owner)
        other = BranchFactory(code="RCB2")
        other_owner = OwnerFactory(branch=other)
        v2 = stocked_variant(other, other_owner, price="1000.00", quantity=5)
        oc2 = login_as(other_owner)
        ec2 = login_as(EmployeeFactory(branch=other))
        sale = ec2.post(
            "/api/v1/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(v2.id), "quantity": 1}],
                "payments": [{"method": "CASH", "amount": "1000.00"}],
            },
            format="json",
        ).json()
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "wrong branch attempt",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 404
        assert res.json()["code"] == "not_found"
        assert oc2  # silence lint

    def test_cannot_link_a_sale_already_linked(self, login_as, branch, owner):
        variant, rec = self._tampered_owner_review(login_as, branch, owner)
        cashier = EmployeeFactory(branch=branch)
        sale = (
            login_as(cashier)
            .post(
                "/api/v1/sales/",
                {
                    "client_sale_id": str(uuid.uuid4()),
                    "items": [{"variant": str(variant.id), "quantity": 2}],
                    "payments": [{"method": "CASH", "amount": "2000.00"}],
                },
                format="json",
            )
            .json()
        )
        first = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "re-entered from the paper till roll",
                "sale_id": sale["id"],
            },
        )
        assert first.status_code == 200, first.content
        # a second held record on the same branch cannot claim the same sale
        rec2 = OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=rec.authorization,
            device=rec.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=99,
            offline_created_at=timezone.now(),
            outcome="OWNER_REVIEW_REQUIRED",
            redacted_payload={
                "items": [{"variant_id": str(variant.id), "quantity": 2}],
                "payments": [
                    {"method": "CASH", "amount": "2000.00", "has_reference": False}
                ],
            },
        )
        res = _reconcile(
            login_as(owner),
            rec2.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "trying to reuse the same sale row",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_already_linked"


# --------------------------------------------------------------------------- #
# immutability + already-resolved                                            #
# --------------------------------------------------------------------------- #


class TestImmutability:
    def test_resolution_cannot_be_changed_afterward(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "returned it all in full",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        # a different kind now fails
        res = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "changed my mind"},
        )
        assert res.status_code == 409
        assert res.json()["code"] == "offline_record_already_resolved"

    def test_same_request_repeated_returns_same_result(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        body = {
            "kind": "REFUNDED_AND_RETURNED",
            "explanation": "full return and refund",
            "all_goods_returned": True,
            "full_amount_refunded": True,
            "refunds": [{"method": "CASH", "amount": "2000.00"}],
        }
        a = _reconcile(login_as(owner), rec.id, body)
        b = _reconcile(login_as(owner), rec.id, body)
        assert a.status_code == b.status_code == 200
        assert OfflineSaleReconciliation.objects.filter(sync_record=rec).count() == 1


# --------------------------------------------------------------------------- #
# security & permissions                                                     #
# --------------------------------------------------------------------------- #


class TestSecurity:
    def test_owner_with_mfa_succeeds_without_mfa_fails(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        no_mfa = login_as(owner, mfa_verified=False).post(
            f"{BASE}/sync-records/{rec.id}/reconcile/",
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "x" * 12,
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
            format="json",
        )
        assert no_mfa.status_code == 403

    def test_cashier_forbidden(self, login_as, branch, owner):
        _v, cashier, _a, rec = _owner_review_record(login_as, branch, owner)
        res = login_as(cashier).post(
            f"{BASE}/sync-records/{rec.id}/reconcile/",
            {"kind": "RECORDED_AS_SALE", "explanation": "should not be allowed"},
            format="json",
        )
        assert res.status_code == 403

    def test_cross_branch_is_indistinguishable_not_found(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        other = BranchFactory(code="RCB9")
        res = _reconcile(
            login_as(OwnerFactory(branch=other)),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "cross branch attempt"},
        )
        assert res.status_code == 404
        assert res.json()["code"] == "not_found"

    def test_reconcile_response_and_lookup_have_no_secret_fields(
        self, login_as, branch, owner
    ):
        _variant, cashier, _auth, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {"kind": "RECORDED_AS_SALE", "explanation": "kept goods and payment"},
        )
        blob = res.content.decode()
        for token in (
            "signed_token",
            "average_unit_cost",
            "unit_cost",
            "OFFLINE_SIGNING",
        ):
            assert token not in blob
        # cashier read: safe status, no owner-only accounting fields
        cid = rec.client_sale_id
        look = login_as(cashier).get(f"{BASE}/sales/{cid}/").json()
        assert look["resolved"] is True
        assert look["resolution_kind"] == "RECORDED_AS_SALE"
        assert "average_unit_cost" not in json_dumps(look)
        assert "cogs" not in json_dumps(look)


def json_dumps(obj):
    import json

    return json.dumps(obj)


# --------------------------------------------------------------------------- #
# session end / queue                                                        #
# --------------------------------------------------------------------------- #


class TestSessionQueue:
    def test_unresolved_blocks_end_resolved_does_not(self, login_as, branch, owner):
        # a still-ACTIVE session with a held CONFLICT record
        variant2, _cashier2, auth2, rec2 = _conflict_record(login_as, branch, owner)
        blocked = login_as(owner).post(
            f"{BASE}/authorizations/{auth2['id']}/end-session/", {}, format="json"
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "unresolved_offline_sales"

        _reconcile(
            login_as(owner),
            rec2.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "counted the shelf, goods left, cash kept",
                "counts": [{"variant": str(variant2.id), "counted_on_hand": 0}],
            },
        )
        ok = login_as(owner).post(
            f"{BASE}/authorizations/{auth2['id']}/end-session/", {}, format="json"
        )
        assert ok.status_code == 200

    def test_force_close_does_not_mark_records_reconciled(
        self, login_as, branch, owner
    ):
        _variant2, _c2, auth2, rec2 = _conflict_record(login_as, branch, owner)
        login_as(owner).post(
            f"{BASE}/authorizations/{auth2['id']}/end-session/",
            {"force": True, "mfa_confirmed": True},
            format="json",
        )
        rec2.refresh_from_db()
        assert rec2.resolved is False
        assert not OfflineSaleReconciliation.objects.filter(sync_record=rec2).exists()


# --------------------------------------------------------------------------- #
# OpenAPI contract                                                           #
# --------------------------------------------------------------------------- #


class TestContract:
    def test_reconcile_endpoint_is_documented(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = "/api/v1/offline/sync-records/{id}/reconcile/"
        assert path in schema["paths"]
        assert "post" in schema["paths"][path]
        comps = schema["components"]["schemas"]
        assert "OfflineReconciliationResult" in comps
        # OfflineSaleLookup gains resolution_kind for the cashier
        assert "resolution_kind" in comps["OfflineSaleLookup"]["properties"]

    def test_owner_read_model_exposes_retained_payments(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        comps = schema["components"]["schemas"]
        # owner-only read model: typed retained-payment projection
        assert "retained_payments" in comps["OfflineSyncRecord"]["properties"]
        rp = comps["_RetainedPayment"]["properties"]
        assert set(rp) == {
            "payment_index",
            "method",
            "amount",
            "reference_required",
        }
        # structured (index, reference) association on the reconcile request
        item = comps["OfflineReconcileRequestRequest"]["properties"][
            "payment_references"
        ]["items"]
        ref = comps[item["$ref"].split("/")[-1]]
        assert set(ref["properties"]) == {"payment_index", "reference"}


# --------------------------------------------------------------------------- #
# Point 1 — LINKED_EXISTING_SALE cannot attach the wrong sale                 #
# --------------------------------------------------------------------------- #


class TestLinkExistingSaleSafety:
    """Every trustworthy retained field (items, quantities, payment
    methods/amounts, verified total) is compared before a link is allowed; a
    mismatch in any comparable field -> 409 ``sale_incompatible``. Only when
    *nothing* is comparable does the owner-attested manual fallback open, and it
    is stamped ``linked_manually`` + ``link_verification: MANUAL_ATTESTED``.
    """

    def test_unrelated_same_branch_sale_is_not_linkable_by_branch_alone(
        self, login_as, branch, owner
    ):
        # retained offline sale: variant A, 2 units, 2000 cash
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        other = stocked_variant(branch, owner, price="1000.00", quantity=10)
        # a completely unrelated completed sale — same branch, same money shape
        sale = _completed_sale(
            login_as, branch, variant=other, quantity=2, amount="2000.00"
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "unrelated sale that only shares the branch and total",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_incompatible"
        rec.refresh_from_db()
        assert rec.resolved is False

    def test_link_rejects_wrong_quantities(self, login_as, branch, owner):
        variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        sale = _completed_sale(
            login_as, branch, variant=variant, quantity=3, amount="3000.00"
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "the quantities do not line up but linking anyway",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_incompatible"

    def test_link_rejects_mismatched_payment_method(self, login_as, branch, owner):
        variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        sale = _completed_sale(
            login_as,
            branch,
            variant=variant,
            quantity=2,
            method="TRANSFER",
            amount="2000.00",
            reference="T-1",
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "same goods and total, different payment method",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_incompatible"

    def test_link_rejects_total_that_differs_from_verified_snapshot(
        self, login_as, branch, owner
    ):
        # items still comparable, payments unreadable -> the verified snapshot
        # total (frozen at 1000/unit) is the deciding check.
        variant, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        rec.redacted_payload = {
            "items": [{"variant_id": str(variant.id), "quantity": 2}],
            "payments": "unreadable",
        }
        rec.save(update_fields=["redacted_payload"])
        # catalogue price moved after the session opened; a fresh sale books high
        set_active_price(variant=variant, amount=Decimal("1500.00"), changed_by=owner)
        sale = _completed_sale(
            login_as, branch, variant=variant, quantity=2, amount="3000.00"
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "items line up but the money is not the verified total",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_incompatible"

    def test_cross_branch_target_is_a_404(self, login_as, branch, owner):
        _v, rec = _tampered_owner_review(login_as, branch, owner)
        other = BranchFactory(code="RCB7")
        other_owner = OwnerFactory(branch=other)
        v2 = stocked_variant(other, other_owner, price="1000.00", quantity=5)
        sale = _completed_sale(
            login_as, other, variant=v2, quantity=2, amount="2000.00"
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "trying to reach across branches",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 404
        assert res.json()["code"] == "not_found"

    def test_already_linked_sale_is_rejected(self, login_as, branch, owner):
        variant, rec = _tampered_owner_review(login_as, branch, owner)
        sale = _completed_sale(
            login_as, branch, variant=variant, quantity=2, amount="2000.00"
        )
        first = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "re-entered from the paper till roll; matches exactly",
                "sale_id": sale["id"],
            },
        )
        assert first.status_code == 200, first.content
        rec2 = OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=rec.authorization,
            device=rec.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=98,
            offline_created_at=timezone.now(),
            outcome="OWNER_REVIEW_REQUIRED",
            redacted_payload={
                "items": [{"variant_id": str(variant.id), "quantity": 2}],
                "payments": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        res = _reconcile(
            login_as(owner),
            rec2.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "trying to reuse a sale that already backs a record",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "sale_already_linked"

    def test_full_match_is_marked_link_verification_full(self, login_as, branch, owner):
        variant, rec = _tampered_owner_review(login_as, branch, owner)
        sale = _completed_sale(
            login_as, branch, variant=variant, quantity=2, amount="2000.00"
        )
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "LINKED_EXISTING_SALE",
                "explanation": "items, payment and total all line up with the paper",
                "sale_id": sale["id"],
            },
        )
        assert res.status_code == 200, res.content
        assert res.json()["reconciliation"]["link_verification"] == "FULL"

    def test_manual_fallback_requires_attestation_and_detailed_explanation(
        self, login_as, branch, owner
    ):
        variant, rec = _tampered_owner_review(login_as, branch, owner)
        rec.redacted_payload = {"items": "gone", "payments": "gone"}
        rec.save(update_fields=["redacted_payload"])
        sale = _completed_sale(
            login_as, branch, variant=variant, quantity=2, amount="2000.00"
        )
        body = {"kind": "LINKED_EXISTING_SALE", "sale_id": sale["id"]}

        r1 = _reconcile(
            login_as(owner), rec.id, {**body, "explanation": "please just link it"}
        )
        assert r1.status_code == 409
        assert r1.json()["code"] == "manual_verification_required"

        r2 = _reconcile(
            login_as(owner),
            rec.id,
            {**body, "owner_attestation": True, "explanation": "verified by hand"},
        )
        assert r2.status_code == 400
        assert r2.json()["code"] == "attestation_explanation_too_short"

        r3 = _reconcile(
            login_as(owner),
            rec.id,
            {
                **body,
                "owner_attestation": True,
                "explanation": (
                    "matched the paper till roll line by line against this sale: "
                    "same two tins, 2000 cash, same date"
                ),
            },
        )
        assert r3.status_code == 200, r3.content
        j = r3.json()["reconciliation"]
        assert j["linked_manually"] is True
        assert j["link_verification"] == "MANUAL_ATTESTED"
        assert j["amount_source"] == "OWNER_ATTESTED"


# --------------------------------------------------------------------------- #
# Point 2 — REFUNDED_AND_RETURNED refunds the money ACTUALLY COLLECTED         #
# --------------------------------------------------------------------------- #


def _rejected_no_payment_record(login_as, branch, owner):
    """A REJECTED `payment_required` record: goods listed, **no payment
    retained** — i.e. no money was actually collected. Built on a live (valid,
    untampered) session so the snapshot total is still recomputable.
    """

    _v, _c, _a, seed = _owner_review_record(login_as, branch, owner)
    rec = OfflineSaleSyncRecord.objects.create(
        branch=branch,
        authorization=seed.authorization,
        device=seed.device,
        client_sale_id=uuid.uuid4(),
        device_sequence=55,
        offline_created_at=timezone.now(),
        outcome="REJECTED",
        detail_code="payment_required",
        redacted_payload={
            "items": [{"variant_id": str(_v.id), "quantity": 2}],
            "payments": [],
        },
    )
    return _v, rec


class TestRefundCollectedAmount:
    def test_retained_payments_matched_to_snapshot(self, login_as, branch, owner):
        # expected 2000, retained 2000, refunded 2000 -> succeeds
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "everything returned, full cash refund given",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert res.status_code == 200, res.content
        recon = res.json()["reconciliation"]
        assert recon["amount_source"] == "RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT"
        assert recon["refund_total"] == "2000.00"
        assert recon["verified_snapshot_total"] == "2000.00"
        assert recon["retained_payments_total"] == "2000.00"

    def test_mismatch_refund_of_catalogue_total_without_attestation_is_rejected(
        self, login_as, branch, owner
    ):
        # expected 2000, retained 1999, refund 2000 without attestation -> rejected
        _v, _c, _a, rec = _rejected_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "full reversal of the whole transaction",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert res.status_code == 409
        assert res.json()["code"] == "offline_total_unverifiable"
        rec.refresh_from_db()
        assert rec.resolved is False

    def test_mismatch_owner_attests_collected_amount_and_refunds_it_exactly(
        self, login_as, branch, owner
    ):
        # expected 2000, retained 1999, owner attests 1999, refund 1999 -> attested
        _v, _c, _a, rec = _rejected_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "till roll and drawer count both show 1999 was taken; that "
                    "exact amount was refunded to the customer in cash"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "1999.00",
                "refunds": [{"method": "CASH", "amount": "1999.00"}],
            },
        )
        assert res.status_code == 200, res.content
        recon = res.json()["reconciliation"]
        assert recon["amount_source"] == "OWNER_ATTESTED"
        assert recon["refund_total"] == "1999.00"
        # owner-only diagnostics preserved side by side
        assert recon["verified_snapshot_total"] == "2000.00"
        assert recon["retained_payments_total"] == "1999.00"

    def test_attested_amount_and_refund_evidence_must_agree(
        self, login_as, branch, owner
    ):
        # attested 1999 but refund evidence totals 2000 -> rejected
        _v, _c, _a, rec = _rejected_record(login_as, branch, owner)
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "the drawer was short; 1999 was collected but the clerk keyed "
                    "a 2000 refund by mistake"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "1999.00",
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "refund_total_mismatch"

    def test_zero_collected_cannot_be_a_fake_refund(self, login_as, branch, owner):
        _v, rec = _rejected_no_payment_record(login_as, branch, owner)
        # positive refund evidence, no attestation -> the amount can't be established
        no_attest = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "goods came back but I want to record a 2000 refund",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert no_attest.status_code == 409
        assert no_attest.json()["code"] == "offline_total_unverifiable"

        # owner truthfully attests nothing was collected -> not a refund at all
        zero = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "the goods were handed over on credit and never paid for; "
                    "they have now been returned in full"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "0.00",
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert zero.status_code == 409
        assert zero.json()["code"] == "no_payment_to_refund"
        rec.refresh_from_db()
        assert rec.resolved is False
        assert not OfflineSaleReconciliation.objects.filter(sync_record=rec).exists()

    def test_diagnostics_and_references_stay_owner_only(self, login_as, branch, owner):
        _v, cashier, _a, rec = _rejected_record(login_as, branch, owner)
        slip = "BANK-SLIP-COLLECTED-1999"
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "counted the drawer against the paper roll: 1999 collected, "
                    "1999 sent straight back to the customer by transfer"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "1999.00",
                "refunds": [
                    {"method": "TRANSFER", "amount": "1999.00", "reference": slip}
                ],
            },
        )
        assert res.status_code == 200, res.content
        recon = res.json()["reconciliation"]
        # owner sees the diagnostics and the reference
        assert recon["verified_snapshot_total"] == "2000.00"
        assert recon["retained_payments_total"] == "1999.00"
        assert recon["refunds"][0]["reference"] == slip

        # no audit row carries the diagnostic keys or the reference
        for row in AuditLog.objects.all():
            for blob in (row.before, row.after):
                assert "verified_snapshot_total" not in blob
                assert "retained_payments_total" not in blob
                assert slip not in json_dumps(blob)

        # the bound cashier's lookup exposes none of it
        look = login_as(cashier).get(f"{BASE}/sales/{rec.client_sale_id}/")
        assert look.status_code == 200
        body = look.json()
        assert "verified_snapshot_total" not in body
        assert "retained_payments_total" not in body
        assert "refunds" not in body
        assert slip not in look.content.decode()

    def test_broken_signature_needs_owner_attested_total(self, login_as, branch, owner):
        _v, rec = _tampered_owner_review(login_as, branch, owner)
        base = {
            "kind": "REFUNDED_AND_RETURNED",
            "all_goods_returned": True,
            "full_amount_refunded": True,
            "refunds": [{"method": "CASH", "amount": "2000.00"}],
        }
        r1 = _reconcile(
            login_as(owner),
            rec.id,
            {**base, "explanation": "customer returned everything"},
        )
        assert r1.status_code == 409
        assert r1.json()["code"] == "offline_total_unverifiable"

        r2 = _reconcile(
            login_as(owner),
            rec.id,
            {**base, "explanation": "x" * 45, "owner_attestation": True},
        )
        assert r2.status_code == 400
        assert r2.json()["code"] == "attested_total_required"

        r3 = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                # >= 10 chars (clears the serializer) but < 40 (fails the
                # owner-attestation evidence bar in the service)
                "explanation": "returned it all",
                "owner_attestation": True,
                "attested_offline_total": "2000.00",
            },
        )
        assert r3.status_code == 400
        assert r3.json()["code"] == "attestation_explanation_too_short"

        r4 = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "explanation": (
                    "counted the paper till roll and the returned tins; the whole "
                    "2000 was refunded in cash at the counter"
                ),
                "owner_attestation": True,
                "attested_offline_total": "2000.00",
            },
        )
        assert r4.status_code == 200, r4.content
        assert r4.json()["reconciliation"]["amount_source"] == "OWNER_ATTESTED"
        assert r4.json()["reconciliation"]["refund_total"] == "2000.00"

    def test_malformed_retained_items_need_owner_attested_total(
        self, login_as, branch, owner
    ):
        _v, _c, _a, rec = _owner_review_record(login_as, branch, owner)
        rec.redacted_payload = {
            "items": [{"variant_id": "x", "quantity": "not-a-number"}],
            "payments": [{"method": "CASH", "amount": "2000.00"}],
        }
        rec.save(update_fields=["redacted_payload"])
        r1 = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "everything came back",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert r1.status_code == 409
        assert r1.json()["code"] == "offline_total_unverifiable"

        r2 = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "reconstructed 2000 from the paper receipt and the returned "
                    "goods on the counter"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "2000.00",
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
        )
        assert r2.status_code == 200, r2.content
        assert r2.json()["reconciliation"]["amount_source"] == "OWNER_ATTESTED"


# --------------------------------------------------------------------------- #
# Point 3 — payment-reference association is structured, not positional       #
# --------------------------------------------------------------------------- #


class TestPaymentReferenceAssociation:
    def test_split_transfer_pos_references_map_by_index_not_order(
        self, login_as, branch, owner
    ):
        _v, _c, _a, rec = _owner_review_multi(
            login_as,
            branch,
            owner,
            qty=4,
            payments=[
                {"method": "TRANSFER", "amount": "2500.00", "reference": "T-DEVICE"},
                {"method": "POS", "amount": "1500.00", "reference": "P-DEVICE"},
            ],
        )
        detail = login_as(owner).get(f"{BASE}/sync-records/{rec.id}/")
        assert detail.json()["retained_payments"] == [
            {
                "payment_index": 0,
                "method": "TRANSFER",
                "amount": "2500.00",
                "reference_required": True,
            },
            {
                "payment_index": 1,
                "method": "POS",
                "amount": "1500.00",
                "reference_required": True,
            },
        ]
        # the device's own references were never stored
        assert "T-DEVICE" not in detail.content.decode()
        assert "P-DEVICE" not in detail.content.decode()

        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "customer kept the goods; bank and card slips attached",
                "payment_references": [
                    {"payment_index": 1, "reference": "POS-SLIP-42"},
                    {"payment_index": 0, "reference": "TRF-99"},
                ],
            },
        )
        assert res.status_code == 200, res.content
        pays = {
            p.method: p.reference
            for p in Payment.objects.filter(sale__client_sale_id=rec.client_sale_id)
        }
        assert pays == {"TRANSFER": "TRF-99", "POS": "POS-SLIP-42"}

    def test_index_errors_are_stable(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_multi(
            login_as,
            branch,
            owner,
            qty=4,
            payments=[
                {"method": "TRANSFER", "amount": "2500.00", "reference": "T"},
                {"method": "POS", "amount": "1500.00", "reference": "P"},
            ],
        )
        base = {
            "kind": "RECORDED_AS_SALE",
            "explanation": "kept the goods, slips attached to the till roll",
        }
        miss = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "payment_references": [{"payment_index": 0, "reference": "TRF-1"}],
            },
        )
        assert miss.status_code == 400
        assert miss.json()["code"] == "reference_required"

        dup = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "payment_references": [
                    {"payment_index": 0, "reference": "A"},
                    {"payment_index": 0, "reference": "B"},
                ],
            },
        )
        assert dup.status_code == 400
        assert dup.json()["code"] == "payment_reference_duplicated"

        oor = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "payment_references": [
                    {"payment_index": 0, "reference": "A"},
                    {"payment_index": 1, "reference": "B"},
                    {"payment_index": 5, "reference": "C"},
                ],
            },
        )
        assert oor.status_code == 400
        assert oor.json()["code"] == "payment_reference_index_invalid"

    def test_only_non_cash_lines_take_a_reference(self, login_as, branch, owner):
        _v, _c, _a, rec = _owner_review_multi(
            login_as,
            branch,
            owner,
            qty=4,
            payments=[
                {"method": "CASH", "amount": "1000.00"},
                {"method": "TRANSFER", "amount": "3000.00", "reference": "BANK"},
            ],
        )
        detail = login_as(owner).get(f"{BASE}/sync-records/{rec.id}/").json()
        assert detail["retained_payments"] == [
            {
                "payment_index": 0,
                "method": "CASH",
                "amount": "1000.00",
                "reference_required": False,
            },
            {
                "payment_index": 1,
                "method": "TRANSFER",
                "amount": "3000.00",
                "reference_required": True,
            },
        ]
        base = {
            "kind": "RECORDED_AS_SALE",
            "explanation": "kept the goods; bank slip covers the transfer portion",
        }
        bad = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "payment_references": [
                    {"payment_index": 0, "reference": "NOPE"},
                    {"payment_index": 1, "reference": "BANK-1"},
                ],
            },
        )
        assert bad.status_code == 400
        assert bad.json()["code"] == "payment_reference_unexpected"

        ok = _reconcile(
            login_as(owner),
            rec.id,
            {
                **base,
                "payment_references": [{"payment_index": 1, "reference": "BANK-1"}],
            },
        )
        assert ok.status_code == 200, ok.content
        pays = {
            p.method: p.reference
            for p in Payment.objects.filter(sale__client_sale_id=rec.client_sale_id)
        }
        assert pays == {"CASH": "", "TRANSFER": "BANK-1"}


# --------------------------------------------------------------------------- #
# Point 4 — historical note-only resolutions are surfaced, not trusted        #
# --------------------------------------------------------------------------- #


class TestUnsafeHistoricalResolutions:
    def test_note_only_resolutions_are_listed_and_reopen_is_audited(
        self, login_as, branch, owner
    ):
        variant, _c, _a, safe = _conflict_record(
            login_as, branch, owner, stock=5, sale_qty=3
        )
        # one record resolved the safe way -> has an OfflineSaleReconciliation
        _reconcile(
            login_as(owner),
            safe.id,
            {
                "kind": "RECORDED_AS_SALE",
                "explanation": "counted the shelf; goods left; cash kept",
                "counts": [{"variant": str(variant.id), "counted_on_hand": 1}],
            },
        )
        # one record mimics the deprecated note-only close: resolved, no recon row
        stale = OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=safe.authorization,
            device=safe.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=77,
            offline_created_at=timezone.now(),
            outcome="CONFLICT",
            detail_code="stock_not_available",
            redacted_payload={
                "items": [{"variant_id": str(variant.id), "quantity": 1}],
                "payments": [
                    {"method": "CASH", "amount": "1000.00", "has_reference": False}
                ],
            },
            resolved=True,
            resolved_by=owner,
            resolved_at=timezone.now(),
            resolution_note="counted stock and moved on",
        )

        flagged = list(unsafe_offline_resolutions())
        assert [r.id for r in flagged] == [stale.id]

        out = StringIO()
        call_command("list_unsafe_offline_resolutions", stdout=out)
        text = out.getvalue()
        assert "Unsafe historical offline resolutions: 1" in text
        assert str(stale.id) in text
        # the note body (may name a customer/amount) is never printed
        assert "counted stock and moved on" not in text

        call_command("list_unsafe_offline_resolutions", "--reopen", stdout=StringIO())
        stale.refresh_from_db()
        assert stale.resolved is False
        assert stale.resolved_by_id is None
        assert stale.resolved_at is None
        assert AuditLog.objects.filter(
            action="offline.reopen_unsafe_resolution", target_id=stale.id
        ).exists()
        # nothing invented: still no sale, no reconciliation, no movement
        assert not OfflineSaleReconciliation.objects.filter(sync_record=stale).exists()
        assert not Sale.objects.filter(client_sale_id=stale.client_sale_id).exists()
        assert unsafe_offline_resolutions().count() == 0


# --------------------------------------------------------------------------- #
# Point 5 — refund references: returned to the owner, redacted everywhere else #
# --------------------------------------------------------------------------- #


class TestRefundReferenceDisclosure:
    def test_owner_sees_refund_reference_audit_and_cashier_do_not(
        self, login_as, branch, owner
    ):
        _v, cashier, _a, rec = _owner_review_record(login_as, branch, owner)
        secret_ref = "BANK-TXN-REF-9931"
        res = _reconcile(
            login_as(owner),
            rec.id,
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "returned everything; refunded in full by bank transfer",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [
                    {"method": "TRANSFER", "amount": "2000.00", "reference": secret_ref}
                ],
            },
        )
        assert res.status_code == 200, res.content
        # 1) owner + MFA reconcile response carries the owner-entered reference back
        assert res.json()["reconciliation"]["refunds"] == [
            {"method": "TRANSFER", "amount": "2000.00", "reference": secret_ref}
        ]
        # 2) no audit row anywhere contains it
        for row in AuditLog.objects.all():
            assert secret_ref not in json_dumps(row.before)
            assert secret_ref not in json_dumps(row.after)
        # 3) the bound cashier's lookup exposes neither refunds nor the reference
        look = login_as(cashier).get(f"{BASE}/sales/{rec.client_sale_id}/")
        assert look.status_code == 200
        assert "refunds" not in look.json()
        assert secret_ref not in look.content.decode()
