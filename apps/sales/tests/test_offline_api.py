"""Stage 16 — offline checkout API: device + authorization + sync + receipts."""

import uuid

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
)
from apps.sales.models import Sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline"


def _register_device(client, name="Till-1"):
    return client.post(f"{BASE}/devices/", {"name": name}, format="json")


def _issue(owner_client, *, device_id, cashier_id):
    return owner_client.post(
        f"{BASE}/authorizations/",
        {"device": str(device_id), "cashier": str(cashier_id)},
        format="json",
    )


def _sale_payload(variant, *, seq=1, qty=2, amount="2000.00", cid=None):
    return {
        "client_sale_id": str(cid or uuid.uuid4()),
        "device_sequence": seq,
        "offline_created_at": timezone.now().isoformat(),
        "items": [{"variant_id": str(variant.id), "quantity": qty}],
        "payments": [{"method": "CASH", "amount": amount}],
    }


class TestDeviceLifecycle:
    def test_owner_registers_device_but_employee_cannot(self, login_as, branch, owner):
        clerk = EmployeeFactory(branch=branch)
        assert _register_device(login_as(clerk)).status_code == 403
        res = _register_device(login_as(owner))
        assert res.status_code == 201
        assert res.json()["status"] == "ACTIVE"

    def test_owner_without_mfa_is_blocked(self, login_as, branch, owner):
        res = _register_device(login_as(owner, mfa_verified=False))
        assert res.status_code == 403

    def test_second_active_device_conflicts(self, login_as, branch, owner):
        c = login_as(owner)
        assert _register_device(c).status_code == 201
        dup = _register_device(c, name="Till-2")
        assert dup.status_code == 409
        assert dup.json()["code"] == "active_device_exists"

    def test_cross_branch_device_is_404(self, login_as, branch, owner):
        other = BranchFactory(code="VS90")
        other_owner = OwnerFactory(branch=other)
        device_id = _register_device(login_as(other_owner)).json()["id"]
        assert (
            login_as(owner).post(f"{BASE}/devices/{device_id}/revoke/").status_code
            == 404
        )


class TestAuthorization:
    def test_owner_issues_and_cashier_reads_status(self, login_as, branch, owner):
        stocked_variant(branch, owner, price="1000.00", quantity=10)
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        device_id = _register_device(oc).json()["id"]
        issued = _issue(oc, device_id=device_id, cashier_id=cashier.id)
        assert issued.status_code == 201
        body = issued.json()
        assert body["status"] == "ACTIVE"
        assert body["signed_token"]

        status = login_as(cashier).get(f"{BASE}/authorizations/{body['id']}/status/")
        assert status.status_code == 200
        assert status.json()["pending_review_count"] == 0

    def test_employee_cannot_issue(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        device_id = _register_device(login_as(owner)).json()["id"]
        res = _issue(login_as(cashier), device_id=device_id, cashier_id=cashier.id)
        assert res.status_code == 403

    def test_cross_branch_authorization_is_404(self, login_as, branch, owner):
        other = BranchFactory(code="VS91")
        other_owner = OwnerFactory(branch=other)
        other_cashier = EmployeeFactory(branch=other)
        stocked_variant(other, other_owner, price="1000.00", quantity=5)
        oc = login_as(other_owner)
        did = _register_device(oc).json()["id"]
        aid = _issue(oc, device_id=did, cashier_id=other_cashier.id).json()["id"]
        assert login_as(owner).get(f"{BASE}/authorizations/{aid}/").status_code == 404


class TestSyncEndpoint:
    def _prepare(self, login_as, branch, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=20)
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        did = _register_device(oc).json()["id"]
        auth = _issue(oc, device_id=did, cashier_id=cashier.id).json()
        return variant, cashier, auth

    def test_cashier_syncs_a_batch_and_gets_official_receipts(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = self._prepare(login_as, branch, owner)
        res = login_as(cashier).post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [_sale_payload(variant, seq=1)],
            },
            format="json",
        )
        assert res.status_code == 200, res.content
        [result] = res.json()["results"]
        assert result["outcome"] == "ACCEPTED"
        assert result["receipt_number"]
        assert Sale.objects.filter(client_sale_id=result["client_sale_id"]).exists()

    def test_wrong_cashier_for_the_token_is_404(self, login_as, branch, owner):
        variant, _cashier, auth = self._prepare(login_as, branch, owner)
        intruder = EmployeeFactory(branch=branch)
        res = login_as(intruder).post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [_sale_payload(variant)],
            },
            format="json",
        )
        assert res.status_code == 404

    def test_tampered_token_is_400_invalid_signature(self, login_as, branch, owner):
        variant, cashier, auth = self._prepare(login_as, branch, owner)
        res = login_as(cashier).post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"] + "x",
                "sales": [_sale_payload(variant)],
            },
            format="json",
        )
        assert res.status_code == 400
        assert res.json()["code"] == "invalid_signature"

    def test_retry_returns_same_receipt(self, login_as, branch, owner):
        variant, cashier, auth = self._prepare(login_as, branch, owner)
        payload = {
            "authorization_token": auth["signed_token"],
            "sales": [_sale_payload(variant, seq=1)],
        }
        c = login_as(cashier)
        first = c.post(f"{BASE}/sync/", payload, format="json").json()["results"][0]
        second = c.post(f"{BASE}/sync/", payload, format="json").json()["results"][0]
        assert first["outcome"] == "ACCEPTED"
        assert second["outcome"] == "DUPLICATE"
        assert second["receipt_number"] == first["receipt_number"]

    def test_temporary_receipt_is_labelled_and_unnumbered(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = self._prepare(login_as, branch, owner)
        res = login_as(cashier).post(
            f"{BASE}/temporary-receipt/",
            {
                "authorization_token": auth["signed_token"],
                "sale": _sale_payload(variant, qty=3, amount="3000.00"),
            },
            format="json",
        )
        assert res.status_code == 200
        body = res.json()
        assert body["label"] == "OFFLINE RECEIPT — PENDING SYNCHRONIZATION"
        assert body["official_receipt_number"] is None
        assert body["total"] == "3000.00"

    def test_client_sale_id_maps_to_official_sale_after_sync(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = self._prepare(login_as, branch, owner)
        cid = uuid.uuid4()
        c = login_as(cashier)
        before = c.get(f"{BASE}/sales/{cid}/")
        assert before.status_code == 404
        c.post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [_sale_payload(variant, cid=cid)],
            },
            format="json",
        )
        after = c.get(f"{BASE}/sales/{cid}/").json()
        assert after["synced"] is True
        assert after["official_receipt_number"]


class TestReviewAndLeakage:
    def _prepare_conflict(self, login_as, branch, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        did = _register_device(oc).json()["id"]
        auth = _issue(oc, device_id=did, cashier_id=cashier.id).json()
        login_as(cashier).post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [
                    _sale_payload(variant, seq=1, qty=3, amount="3000.00"),
                    _sale_payload(variant, seq=2, qty=3, amount="3000.00"),
                ],
            },
            format="json",
        )
        return oc

    def test_owner_lists_and_resolves_a_conflict_record(self, login_as, branch, owner):
        oc = self._prepare_conflict(login_as, branch, owner)
        listed = oc.get(f"{BASE}/sync-records/?outcome=CONFLICT").json()
        assert listed["count"] == 1
        record_id = listed["results"][0]["id"]
        resolved = oc.post(
            f"{BASE}/sync-records/{record_id}/resolve/",
            {"note": "counted stock, wrote off shrinkage"},
            format="json",
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved"] is True

    def test_sync_record_response_hides_phone_reference_and_token(
        self, login_as, branch, owner
    ):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        did = _register_device(oc).json()["id"]
        auth = _issue(oc, device_id=did, cashier_id=cashier.id).json()
        payload = _sale_payload(variant)
        payload["customer_phone"] = "08099999999"
        payload["payments"] = [
            {"method": "POS", "amount": "2000.00", "reference": "SECRET-REF-123"}
        ]
        login_as(cashier).post(
            f"{BASE}/sync/",
            {"authorization_token": auth["signed_token"], "sales": [payload]},
            format="json",
        )
        blob = oc.get(f"{BASE}/sync-records/").content.decode()
        assert "08099999999" not in blob
        assert "SECRET-REF-123" not in blob
        assert auth["signed_token"][:40] not in blob

    def test_authorization_snapshot_exposes_no_cost(self, login_as, branch, owner):
        stocked_variant(branch, owner, price="1000.00", quantity=10, unit_cost="640.00")
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        did = _register_device(oc).json()["id"]
        aid = _issue(oc, device_id=did, cashier_id=cashier.id).json()["id"]
        blob = oc.get(f"{BASE}/authorizations/{aid}/").content.decode()
        assert "640.00" not in blob
        assert "unit_cost" not in blob
        assert "average_unit_cost" not in blob
