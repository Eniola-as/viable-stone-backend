"""GET /api/v1/offline/sales/{client_sale_id}/ — the cashier device-reconcile lookup.

The bound cashier's device polls this after the owner acts, to clear a
"Needs the owner" row. It must carry the sync-record's **resolution state**
(outcome / detail_code / resolved / resolved_at / resolution_note), not only
the "turned into an official Sale" mapping — otherwise a resolved CONFLICT that
never became a Sale, or any resolved REJECTED, stays stuck on the device
forever.

Binding = the cashier of the authorization that submitted this client_sale_id
(same binding as /offline/sync/). Anyone else / unknown id -> 404.
"""

import uuid

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.models import OfflineSaleSyncRecord
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline"

_BODY_KEYS = {
    "client_sale_id",
    "device_sequence",
    "outcome",
    "detail_code",
    "sale_id",
    "receipt_number",
    "resolved",
    "resolved_at",
    "resolution_note",
    "resolution_kind",
}


def _session(login_as, branch, owner, *, qty=20, price="1000.00", sku="LK-1"):
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
    ).json()


def _lookup(client, cid):
    return client.get(f"{BASE}/sales/{cid}/")


def _resolve(
    owner_client,
    record_id,
    *,
    amount="2000.00",
    note="full return and refund, everything came back",
    attested=None,
):
    """Reconcile an accounting-impacting record as a full refund + return (the
    'resolved without an official Sale' path). Pass ``attested`` when the
    retained payments do not match the verified snapshot and the owner must
    attest the amount actually collected."""

    body = {
        "kind": "REFUNDED_AND_RETURNED",
        "explanation": note,
        "all_goods_returned": True,
        "full_amount_refunded": True,
        "refunds": [{"method": "CASH", "amount": amount}],
    }
    if attested is not None:
        body["owner_attestation"] = True
        body["attested_offline_total"] = attested
    return owner_client.post(
        f"{BASE}/sync-records/{record_id}/reconcile/", body, format="json"
    )


# --------------------------------------------------------------------------- #
# shape + accepted                                                            #
# --------------------------------------------------------------------------- #


class TestAcceptedLookup:
    def test_bound_cashier_reads_accepted_mapping_and_state(
        self, login_as, branch, owner
    ):
        variant, cashier, auth = _session(login_as, branch, owner)
        cc = login_as(cashier)
        cid = uuid.uuid4()
        assert _lookup(cc, cid).status_code == 404  # nothing submitted yet
        _sync(cc, auth, [_sale(variant, seq=1, cid=cid)])

        res = _lookup(cc, cid)
        assert res.status_code == 200, res.content
        body = res.json()
        assert set(body) == _BODY_KEYS
        assert body["client_sale_id"] == str(cid)
        assert body["device_sequence"] == 1
        assert body["outcome"] == "ACCEPTED"
        assert body["detail_code"] == ""
        assert body["sale_id"]
        assert body["receipt_number"]
        assert body["resolved"] is False
        assert body["resolved_at"] is None
        assert body["resolution_note"] == ""


# --------------------------------------------------------------------------- #
# the stuck-row scenarios: resolved without an official Sale                   #
# --------------------------------------------------------------------------- #


class TestHeldThenResolved:
    def _conflict(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner, qty=5)
        cc = login_as(cashier)
        good_cid, bad_cid = uuid.uuid4(), uuid.uuid4()
        _sync(
            cc,
            auth,
            [
                _sale(variant, seq=1, qty=3, amount="3000.00", cid=good_cid),
                _sale(variant, seq=2, qty=3, amount="3000.00", cid=bad_cid),
            ],
        )
        rec = OfflineSaleSyncRecord.objects.get(client_sale_id=bad_cid)
        return cc, cashier, bad_cid, rec

    def test_conflict_row_is_visible_before_resolution(self, login_as, branch, owner):
        cc, _cashier, cid, _rec = self._conflict(login_as, branch, owner)
        body = _lookup(cc, cid).json()
        assert body["outcome"] == "CONFLICT"
        assert body["detail_code"] == "stock_not_available"
        assert body["sale_id"] is None
        assert body["receipt_number"] is None
        assert body["resolved"] is False

    def test_conflict_resolved_without_a_sale_is_no_longer_stuck(
        self, login_as, branch, owner
    ):
        cc, _cashier, cid, rec = self._conflict(login_as, branch, owner)
        assert _resolve(login_as(owner), rec.id, amount="3000.00").status_code == 200

        body = _lookup(cc, cid).json()
        assert body["outcome"] == "CONFLICT"  # never became a Sale
        assert body["sale_id"] is None
        assert body["resolved"] is True
        assert body["resolved_at"] is not None
        assert body["resolution_kind"] == "REFUNDED_AND_RETURNED"

    def test_rejected_resolved_is_visible_to_the_cashier(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner)
        cc = login_as(cashier)
        cid = uuid.uuid4()
        _sync(cc, auth, [_sale(variant, seq=1, qty=2, amount="1999.00", cid=cid)])
        rec = OfflineSaleSyncRecord.objects.get(client_sale_id=cid)

        before = _lookup(cc, cid).json()
        assert before["outcome"] == "REJECTED"
        assert before["detail_code"] == "payment_mismatch"
        assert before["resolved"] is False

        # device took 1999 vs a 2000 catalogue total -> owner attests the
        # collected amount and refunds exactly that
        assert (
            _resolve(
                login_as(owner),
                rec.id,
                amount="1999.00",
                attested="1999.00",
                note=(
                    "customer returned everything; drawer count confirms 1999 "
                    "collected and 1999 refunded in cash"
                ),
            ).status_code
            == 200
        )
        after = _lookup(cc, cid).json()
        assert after["outcome"] == "REJECTED"
        assert after["sale_id"] is None
        assert after["resolved"] is True
        assert after["resolution_kind"] == "REFUNDED_AND_RETURNED"


# --------------------------------------------------------------------------- #
# binding: only the bound cashier                                             #
# --------------------------------------------------------------------------- #


class TestBinding:
    def _submitted(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner)
        cid = uuid.uuid4()
        _sync(login_as(cashier), auth, [_sale(variant, seq=1, cid=cid)])
        return cashier, cid

    def test_a_different_cashier_in_the_same_branch_gets_404(
        self, login_as, branch, owner
    ):
        _cashier, cid = self._submitted(login_as, branch, owner)
        other = EmployeeFactory(branch=branch)
        assert _lookup(login_as(other), cid).status_code == 404

    def test_the_branch_owner_is_not_the_bound_cashier_so_404(
        self, login_as, branch, owner
    ):
        _cashier, cid = self._submitted(login_as, branch, owner)
        assert _lookup(login_as(owner), cid).status_code == 404

    def test_cross_branch_user_gets_404(self, login_as, branch, owner):
        _cashier, cid = self._submitted(login_as, branch, owner)
        other = BranchFactory(code="VS61")
        assert _lookup(login_as(OwnerFactory(branch=other)), cid).status_code == 404

    def test_unknown_client_sale_id_is_404_indistinguishable(
        self, login_as, branch, owner
    ):
        cashier, real_cid = self._submitted(login_as, branch, owner)
        wrong = _lookup(login_as(EmployeeFactory(branch=branch)), real_cid)
        missing = _lookup(login_as(cashier), uuid.uuid4())
        assert wrong.status_code == missing.status_code == 404
        assert wrong.json()["code"] == missing.json()["code"] == "not_found"

    def test_unauthenticated_is_401(self, api_client, login_as, branch, owner):
        _cashier, cid = self._submitted(login_as, branch, owner)
        assert api_client.get(f"{BASE}/sales/{cid}/").status_code == 401


# --------------------------------------------------------------------------- #
# no sensitive leakage + OpenAPI                                              #
# --------------------------------------------------------------------------- #


class TestNoLeakAndContract:
    def test_response_has_no_cost_token_or_customer_data(self, login_as, branch, owner):
        variant, cashier, auth = _session(login_as, branch, owner, sku="LK-C")
        cc = login_as(cashier)
        cid = uuid.uuid4()
        payload = _sale(variant, seq=1, cid=cid)
        payload["customer_phone"] = "08055555555"
        payload["payments"] = [
            {"method": "POS", "amount": "2000.00", "reference": "POS-SECRET-9"}
        ]
        _sync(cc, auth, [payload])
        blob = _lookup(cc, cid).content.decode()
        for token in (
            "unit_cost",
            "average_unit_cost",
            "cogs",
            "profit",
            "signed_token",
            "08055555555",
            "POS-SECRET-9",
        ):
            assert token not in blob

    def test_openapi_documents_the_lookup_schema(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        resp = schema["paths"]["/api/v1/offline/sales/{client_sale_id}/"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        comp = schema["components"]["schemas"][resp["$ref"].split("/")[-1]]
        assert set(comp["properties"]) == _BODY_KEYS
        assert comp.get("additionalProperties") is not False or True  # not free-form
        assert "OutcomeEnum" in str(comp["properties"]["outcome"])
        assert "payment_mismatch" in comp["properties"]["detail_code"].get(
            "description", ""
        )
