"""G23 + offline sync-record review completeness + the detail_code catalogue.

1. ``POST /offline/sync/`` returns ``{"results": [...]}`` — an **object** with a
   ``results`` array, never the bare array the old schema promised. Lock the
   wrapper shape (runtime unchanged).
2. ``pending_review_count`` (and the plain end-session block, and
   ``resolve_sync_record``) now include unresolved **REJECTED** records — a
   rejected offline sale is a real sale the device already took money for; the
   owner must reconcile it, so it belongs in the review queue.
3. Every ``detail_code`` sync can return is enumerated on ``OfflineSyncResult``.
"""

import uuid

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import EmployeeFactory
from apps.sales.models import OfflineSaleSyncRecord
from apps.sales.services.offline import (
    end_offline_session,
    pending_review_count,
    resolve_sync_record,
)
from apps.sales.services.offline_reconciliation import reconcile_sync_record
from apps.sales.tests.factories import stocked_variant


def _full_refund_reconcile(rec, owner, amount="1999.00"):
    # The rejected session below took 1999 (device) against a 2000 catalogue
    # total, so the collected amount is owner-attested and the refund matches it.
    return reconcile_sync_record(
        record=rec,
        owner=owner,
        kind="REFUNDED_AND_RETURNED",
        explanation=(
            "customer returned every item; the drawer count confirms 1999 was "
            "collected and that exact amount was refunded to them in cash"
        ),
        all_goods_returned=True,
        full_amount_refunded=True,
        owner_attestation=True,
        attested_offline_total=amount,
        refunds=[{"method": "CASH", "amount": amount, "reference": ""}],
    )


pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline"

# The complete set of detail_code values sync can put on a result / record.
_REJECTED_CODES = {
    "duplicate_sequence",
    "outside_window",
    "invalid_timestamp",
    "empty_cart",
    "price_not_in_snapshot",
    "invalid_quantity",
    "payment_required",
    "invalid_payment_method",
    "invalid_payment_amount",
    "reference_required",
    "invalid_tendered_amount",
    "payment_mismatch",
    "variant_not_found",
    "variant_unavailable",
}
_CONFLICT_CODES = {"stock_not_available", "sale_in_progress"}
_REVIEW_CODES = {"revoked", "replaced", "force_closed"}
_ALL_DETAIL_CODES = _REJECTED_CODES | _CONFLICT_CODES | _REVIEW_CODES | {""}


def _session(login_as, branch, owner, *, price="1000.00", qty=20, sku="SC-1"):
    variant = stocked_variant(branch, owner, price=price, quantity=qty, sku=sku)
    cashier = EmployeeFactory(branch=branch)
    oc = login_as(owner)
    device_id = oc.post(f"{BASE}/devices/", {"name": "Till-1"}, format="json").json()[
        "id"
    ]
    auth = oc.post(
        f"{BASE}/authorizations/",
        {"device": device_id, "cashier": str(cashier.id)},
        format="json",
    ).json()
    return variant, cashier, auth


def _sale(variant, *, seq=1, qty=2, amount="2000.00", cid=None):
    return {
        "client_sale_id": str(cid or uuid.uuid4()),
        "device_sequence": seq,
        "offline_created_at": timezone.now().isoformat(),
        "items": [{"variant_id": str(variant.id), "quantity": qty}],
        "payments": [{"method": "CASH", "amount": amount}],
    }


def _sync(client, auth, sales):
    return client.post(
        f"{BASE}/sync/",
        {"authorization_token": auth["signed_token"], "sales": sales},
        format="json",
    )


# --------------------------------------------------------------------------- #
# 1. G23 — response is an object with `results`, not a bare array              #
# --------------------------------------------------------------------------- #


class TestSyncResponseShape:
    def test_one_sale_batch_returns_results_object(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner)
        res = _sync(login_as(cashier), auth, [_sale(variant, seq=1)])
        assert res.status_code == 200, res.content
        body = res.json()
        assert isinstance(body, dict)
        assert list(body) == ["results"]
        assert isinstance(body["results"], list) and len(body["results"]) == 1

    def test_multi_sale_batch_returns_results_object(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner)
        res = _sync(
            login_as(cashier),
            auth,
            [_sale(variant, seq=1), _sale(variant, seq=2)],
        )
        body = res.json()
        assert isinstance(body, dict) and len(body["results"]) == 2

    def test_openapi_declares_the_results_wrapper_object(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        resp = schema["paths"]["/api/v1/offline/sync/"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        ref = schema["components"]["schemas"][resp["$ref"].split("/")[-1]]
        assert ref["type"] == "object"
        assert "results" in ref["properties"]
        assert ref["properties"]["results"]["type"] == "array"


# --------------------------------------------------------------------------- #
# 2. pending_review_count / end-session / resolve include REJECTED             #
# --------------------------------------------------------------------------- #


class TestRejectedCountsAsPendingReview:
    def _rejected_session(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner)
        # payment_mismatch -> REJECTED (device took ₦1,999 for a ₦2,000 sale)
        res = _sync(
            login_as(cashier),
            auth,
            [_sale(variant, seq=1, qty=2, amount="1999.00")],
        )
        [result] = res.json()["results"]
        assert result["outcome"] == "REJECTED"
        assert result["detail_code"] == "payment_mismatch"
        rec = OfflineSaleSyncRecord.objects.get(client_sale_id=result["client_sale_id"])
        return auth, rec

    def test_status_endpoint_counts_unresolved_rejected(self, login_as, branch, owner):
        auth, _rec = self._rejected_session(login_as, branch, owner)
        body = login_as(owner).get(f"{BASE}/authorizations/{auth['id']}/status/").json()
        assert body["pending_review_count"] >= 1

    def test_pending_review_count_service_counts_rejected(
        self, login_as, branch, owner
    ):
        _auth, rec = self._rejected_session(login_as, branch, owner)
        assert pending_review_count(rec.authorization) == 1
        # a bare note can no longer clear it
        import pytest as _pt

        with _pt.raises(Exception) as err:
            resolve_sync_record(record=rec, owner=owner, note="re-rang online")
        assert getattr(err.value, "code", "") == "offline_reconciliation_required"
        assert pending_review_count(rec.authorization) == 1
        # a structured reconciliation clears it
        _full_refund_reconcile(rec, owner)
        assert pending_review_count(rec.authorization) == 0

    def test_owner_can_reconcile_a_rejected_record_via_api(
        self, login_as, branch, owner
    ):
        _auth, rec = self._rejected_session(login_as, branch, owner)
        note_only = login_as(owner).post(
            f"{BASE}/sync-records/{rec.id}/resolve/",
            {"note": "reconciled manually, drawer short 1 naira"},
            format="json",
        )
        assert note_only.status_code == 409
        assert note_only.json()["code"] == "offline_reconciliation_required"
        # the device recorded 1999 against a 2000 catalogue total, so the amount
        # actually collected is owner-attested and the refund must equal it
        blind = login_as(owner).post(
            f"{BASE}/sync-records/{rec.id}/reconcile/",
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": "customer returned everything and was refunded",
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
            format="json",
        )
        assert blind.status_code == 409
        assert blind.json()["code"] == "offline_total_unverifiable"

        res = login_as(owner).post(
            f"{BASE}/sync-records/{rec.id}/reconcile/",
            {
                "kind": "REFUNDED_AND_RETURNED",
                "explanation": (
                    "customer returned every item; drawer count confirms 1999 "
                    "was collected and 1999 was refunded in cash"
                ),
                "all_goods_returned": True,
                "full_amount_refunded": True,
                "owner_attestation": True,
                "attested_offline_total": "1999.00",
                "refunds": [{"method": "CASH", "amount": "1999.00"}],
            },
            format="json",
        )
        assert res.status_code == 200, res.content
        assert res.json()["sync_record"]["resolved"] is True
        assert res.json()["reconciliation"]["amount_source"] == "OWNER_ATTESTED"

    def test_plain_end_session_is_blocked_by_an_unresolved_rejected_record(
        self, login_as, branch, owner
    ):
        _auth, rec = self._rejected_session(login_as, branch, owner)
        obj = rec.authorization
        with pytest.raises(Exception) as err:
            end_offline_session(authorization=obj, owner=owner)
        assert getattr(err.value, "code", "") == "unresolved_offline_sales"
        _full_refund_reconcile(rec, owner)
        end_offline_session(authorization=obj, owner=owner)
        obj.refresh_from_db()
        assert obj.status == "CLOSED"


# --------------------------------------------------------------------------- #
# 3. detail_code catalogue                                                     #
# --------------------------------------------------------------------------- #


class TestDetailCodeCatalogue:
    def test_observed_detail_codes_are_all_in_the_documented_set(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = _session(login_as, branch, owner, qty=5)
        cc = login_as(cashier)
        res = _sync(
            cc,
            auth,
            [
                _sale(variant, seq=1, qty=3, amount="3000.00"),  # ACCEPTED
                _sale(variant, seq=2, qty=3, amount="3000.00"),  # CONFLICT stock
                _sale(variant, seq=3, qty=1, amount="999.00"),  # REJECTED mismatch
            ],
        )
        by_seq = {r["device_sequence"]: r for r in res.json()["results"]}
        assert by_seq[1]["outcome"] == "ACCEPTED" and by_seq[1]["detail_code"] == ""
        assert by_seq[2]["detail_code"] == "stock_not_available"
        assert by_seq[3]["detail_code"] == "payment_mismatch"
        for r in by_seq.values():
            assert r["detail_code"] in _ALL_DETAIL_CODES

    def test_openapi_documents_detail_code_and_outcome(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        result = schema["components"]["schemas"]["OfflineSyncResult"]["properties"]
        desc = result["detail_code"].get("description", "")
        assert "payment_mismatch" in desc
        assert "stock_not_available" in desc
        # outcome is a documented enum, not a bare string
        assert "$ref" in str(result["outcome"]) or "enum" in str(result["outcome"])
