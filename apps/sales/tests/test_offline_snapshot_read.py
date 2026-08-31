"""Stage 16 follow-up (frontend gap G2) — the bound cashier READS the signed
fixed-price catalogue snapshot for the active offline session.

Stage 16 already builds, signs and stores the snapshot and consumes it
server-side in ``/offline/sync/`` and ``/offline/temporary-receipt/``; it just
had no read contract for the disconnected device that needs to *build* an
offline sale. This suite covers the smallest additive endpoint:

    GET /api/v1/offline/authorizations/{id}/snapshot/

bound to the authenticated cashier, on the active registered device, in the
owning branch, using the existing Stage 16 signed-token verification.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator

from apps.accounts.models import OfflineDeviceAuthorization
from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
    RegisteredDeviceFactory,
)
from apps.catalog.services.pricing import set_active_price
from apps.core.exceptions import APIError
from apps.inventory.models import MovementType
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import OfflineSaleSyncRecord, OfflineSyncOutcome, Sale
from apps.sales.services.offline import (
    catalogue_snapshot_for_cashier,
    end_offline_session,
    issue_offline_authorization,
    replace_offline_authorization,
    revoke_offline_authorization,
)
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline"
ROOT = Path(settings.BASE_DIR)

_ITEM_KEYS = {"variant_id", "name", "sku", "price", "quantity", "low_stock_level"}
_ENVELOPE_KEYS = {"code", "message", "field_errors", "request_id"}
_FORBIDDEN_SUBSTRINGS = (
    "unit_cost",
    "average_unit_cost",
    "average_cost",
    "supplier_cost",
    "stock_value",
    "cogs",
    "profit",
    "OFFLINE_SIGNING_KEY",
)


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _v(sku, price, quantity, *, low=1, cost="100.00"):
    """Kwargs for ``stocked_variant`` — keeps the call sites terse."""

    return {
        "sku": sku,
        "price": price,
        "quantity": quantity,
        "low_stock_level": low,
        "unit_cost": cost,
    }


def _make_variants(branch, owner, variants):
    return {v["sku"]: stocked_variant(branch, owner, **v) for v in variants}


def _open_session_api(login_as, branch, owner, *, variants):
    """Register a device + issue an authorization through the real API.

    Returns ``(made, cashier, auth_json, device_id)``.
    """

    made = _make_variants(branch, owner, variants)
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
    return made, cashier, auth, device_id


def _open_session_service(branch, owner, *, variants):
    made = _make_variants(branch, owner, variants)
    cashier = EmployeeFactory(branch=branch)
    auth = issue_offline_authorization(
        branch=branch,
        device=RegisteredDeviceFactory(branch=branch),
        cashier=cashier,
        owner=owner,
    )
    return made, cashier, auth


def _snapshot_url(auth_id):
    return f"{BASE}/authorizations/{auth_id}/snapshot/"


# --------------------------------------------------------------------------- #
# success                                                                     #
# --------------------------------------------------------------------------- #


class TestSuccess:
    def test_authorised_cashier_on_registered_device_gets_the_snapshot(
        self, login_as, branch, owner
    ):
        made, cashier, auth, _did = _open_session_api(
            login_as,
            branch,
            owner,
            variants=[
                _v("WS-WHT-20L", "48500.00", 12, low=6),
                _v("AAA-1L", "1999.99", 7, low=3),
            ],
        )
        res = login_as(cashier).get(_snapshot_url(auth["id"]))
        assert res.status_code == 200, res.content
        body = res.json()

        assert body["authorization_id"] == auth["id"]
        assert body["status"] == "ACTIVE"
        assert body["snapshot_version"] == auth["snapshot_version"]
        assert body["issued_at"] and body["expires_at"]
        # the proof the existing sync protocol needs, verbatim
        assert body["signed_token"] == auth["signed_token"]

        by_sku = {row["sku"]: row for row in body["items"]}
        assert set(by_sku) == {"WS-WHT-20L", "AAA-1L"}
        for row in by_sku.values():
            assert set(row) == _ITEM_KEYS
        white = by_sku["WS-WHT-20L"]
        assert white["variant_id"] == str(made["WS-WHT-20L"].id)
        assert white["name"] == made["WS-WHT-20L"].product.name
        assert white["low_stock_level"] == 6

    def test_exact_decimal_prices_and_whole_number_quantities(
        self, login_as, branch, owner
    ):
        _made, cashier, auth, _did = _open_session_api(
            login_as,
            branch,
            owner,
            variants=[
                _v("WS-WHT-20L", "48500.00", 12, low=6),
                _v("CENT-1", "0.10", 1, low=1),
                _v("ODD-1", "1999.99", 250, low=9),
            ],
        )
        rows = login_as(cashier).get(_snapshot_url(auth["id"])).json()["items"]
        items = {r["sku"]: r for r in rows}

        assert items["WS-WHT-20L"]["price"] == "48500.00"
        assert items["CENT-1"]["price"] == "0.10"
        assert items["ODD-1"]["price"] == "1999.99"
        for row in items.values():
            assert isinstance(row["price"], str)
            assert isinstance(row["quantity"], int)
        assert items["WS-WHT-20L"]["quantity"] == 12
        assert items["ODD-1"]["quantity"] == 250

    def test_response_carries_private_no_store_cache_headers(
        self, login_as, branch, owner
    ):
        _made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("A-1", "100.00", 3)]
        )
        res = login_as(cashier).get(_snapshot_url(auth["id"]))
        assert res["Cache-Control"] == "private, no-store"


# --------------------------------------------------------------------------- #
# the snapshot is fixed for the session lifetime                              #
# --------------------------------------------------------------------------- #


class TestStability:
    def test_unchanged_after_live_price_and_stock_changes(
        self, login_as, branch, owner
    ):
        made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("PNT-1", "1000.00", 20, low=4)]
        )
        cc = login_as(cashier)
        first = cc.get(_snapshot_url(auth["id"])).json()

        variant = made["PNT-1"]
        set_active_price(variant=variant, amount=Decimal("9999.00"), changed_by=owner)
        balance = lock_balances(branch, [variant.id])[variant.id]
        write_movement(
            balance=balance,
            delta=-15,
            movement_type=MovementType.ADJUSTMENT,
            reference_type="adjustment",
            reference_id=uuid.uuid4(),
        )

        second = cc.get(_snapshot_url(auth["id"])).json()
        assert second == first
        assert second["items"][0]["price"] == "1000.00"
        assert second["items"][0]["quantity"] == 20

    def test_repeated_retrieval_is_byte_identical(self, login_as, branch, owner):
        _made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("R-1", "750.00", 5, low=2)]
        )
        cc = login_as(cashier)
        payloads = [cc.get(_snapshot_url(auth["id"])).content for _ in range(3)]
        assert payloads[0] == payloads[1] == payloads[2]

    def test_served_snapshot_ignores_a_mutated_stored_snapshot_column(
        self, branch, owner
    ):
        made, cashier, auth = _open_session_service(
            branch, owner, variants=[_v("COL-1", "1234.00", 8, low=2)]
        )
        auth.snapshot = {
            "version": 1,
            "branch_id": str(branch.id),
            "variants": [
                {
                    "variant_id": str(made["COL-1"].id),
                    "name": "HACKED NAME",
                    "sku": "COL-1",
                    "price": "0.01",
                    "quantity": 999999,
                    "low_stock_level": 0,
                }
            ],
        }
        auth.save(update_fields=["snapshot"])

        served = catalogue_snapshot_for_cashier(authorization=auth, user=cashier)
        row = served["items"][0]
        assert row["name"] == made["COL-1"].product.name
        assert row["price"] == "1234.00"
        assert row["quantity"] == 8


# --------------------------------------------------------------------------- #
# no cost / secret / customer leakage                                         #
# --------------------------------------------------------------------------- #


class TestNoLeakage:
    def test_no_cost_secret_or_customer_fields_in_the_response(
        self, login_as, branch, owner
    ):
        _made, cashier, auth, _did = _open_session_api(
            login_as,
            branch,
            owner,
            variants=[_v("LEAK-1", "4000.00", 10, low=3, cost="31337.00")],
        )
        blob = login_as(cashier).get(_snapshot_url(auth["id"])).content.decode()
        assert "31337" not in blob
        for token in _FORBIDDEN_SUBSTRINGS:
            assert token not in blob
        assert settings.OFFLINE_SIGNING_KEY not in blob
        assert "phone" not in blob
        assert "password" not in blob


# --------------------------------------------------------------------------- #
# wrong cashier / device / branch -> 404, no existence disclosure             #
# --------------------------------------------------------------------------- #


class TestAccessControl:
    def test_other_employee_in_the_same_branch_is_404(self, login_as, branch, owner):
        _made, _cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("X-1", "100.00", 3)]
        )
        intruder = EmployeeFactory(branch=branch)
        assert login_as(intruder).get(_snapshot_url(auth["id"])).status_code == 404

    def test_the_branch_owner_is_not_the_bound_cashier_so_404(
        self, login_as, branch, owner
    ):
        _made, _cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("X-2", "100.00", 3)]
        )
        assert login_as(owner).get(_snapshot_url(auth["id"])).status_code == 404

    def test_cross_branch_user_is_404(self, login_as, branch, owner):
        _made, _cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("X-3", "100.00", 3)]
        )
        other = BranchFactory(code="VS77")
        other_owner = OwnerFactory(branch=other)
        assert login_as(other_owner).get(_snapshot_url(auth["id"])).status_code == 404

    def test_revoked_device_is_404(self, login_as, branch, owner):
        _made, cashier, auth, device_id = _open_session_api(
            login_as, branch, owner, variants=[_v("X-4", "100.00", 3)]
        )
        login_as(owner).post(f"{BASE}/devices/{device_id}/revoke/")
        assert login_as(cashier).get(_snapshot_url(auth["id"])).status_code == 404

    def test_wrong_cashier_looks_exactly_like_a_missing_authorization(
        self, login_as, branch, owner
    ):
        _made, _cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("X-5", "100.00", 3)]
        )
        intruder = login_as(EmployeeFactory(branch=branch))
        wrong_cashier = intruder.get(_snapshot_url(auth["id"]))
        missing = intruder.get(_snapshot_url(uuid.uuid4()))
        assert wrong_cashier.status_code == missing.status_code == 404
        assert wrong_cashier.json()["code"] == missing.json()["code"]
        assert set(wrong_cashier.json()) == _ENVELOPE_KEYS

    def test_unauthenticated_is_401(self, api_client, login_as, branch, owner):
        _made, _cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("X-6", "100.00", 3)]
        )
        assert api_client.get(_snapshot_url(auth["id"])).status_code == 401


# --------------------------------------------------------------------------- #
# expired / revoked / replaced / force-closed -> safe error envelope          #
# --------------------------------------------------------------------------- #


class TestDeadSessions:
    def _session(self, login_as, branch, owner):
        _made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("D-1", "500.00", 6, low=2)]
        )
        return cashier, OfflineDeviceAuthorization.objects.get(id=auth["id"])

    def test_expired_authorization_returns_the_standard_envelope(
        self, login_as, branch, owner
    ):
        cashier, auth = self._session(login_as, branch, owner)
        auth.expires_at = timezone.now() - timedelta(minutes=1)
        auth.save(update_fields=["expires_at"])
        res = login_as(cashier).get(_snapshot_url(auth.id))
        assert res.status_code == 409
        assert res.json()["code"] == "offline_authorization_expired"
        assert set(res.json()) == _ENVELOPE_KEYS

    def test_revoked_authorization_returns_the_standard_envelope(
        self, login_as, branch, owner
    ):
        cashier, auth = self._session(login_as, branch, owner)
        revoke_offline_authorization(authorization=auth, owner=owner, reason="lost")
        res = login_as(cashier).get(_snapshot_url(auth.id))
        assert res.status_code == 409
        assert res.json()["code"] == "offline_session_not_active"

    def test_replaced_authorization_returns_the_standard_envelope(
        self, login_as, branch, owner
    ):
        cashier, auth = self._session(login_as, branch, owner)
        replace_offline_authorization(authorization=auth, owner=owner)
        res = login_as(cashier).get(_snapshot_url(auth.id))
        assert res.status_code == 409
        assert res.json()["code"] == "offline_session_not_active"

    def test_force_closed_authorization_returns_the_standard_envelope(
        self, login_as, branch, owner
    ):
        cashier, auth = self._session(login_as, branch, owner)
        OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=auth,
            device=auth.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=1,
            offline_created_at=timezone.now(),
            outcome=OfflineSyncOutcome.CONFLICT,
        )
        end_offline_session(
            authorization=auth, owner=owner, force=True, reason="closing shop"
        )
        res = login_as(cashier).get(_snapshot_url(auth.id))
        assert res.status_code == 409
        assert res.json()["code"] == "offline_session_not_active"


# --------------------------------------------------------------------------- #
# tampered snapshot / proof rejection                                         #
# --------------------------------------------------------------------------- #


class TestTampering:
    def test_appended_byte_on_the_signed_token_is_rejected(self, branch, owner):
        _made, cashier, auth = _open_session_service(
            branch, owner, variants=[_v("T-1", "100.00", 3)]
        )
        auth.signed_token = auth.signed_token + "x"
        auth.save(update_fields=["signed_token"])
        with pytest.raises(APIError) as err:
            catalogue_snapshot_for_cashier(authorization=auth, user=cashier)
        assert err.value.code == "invalid_signature"

    def test_body_edit_of_the_signed_token_is_rejected_over_http(
        self, login_as, branch, owner
    ):
        _made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("T-2", "100.00", 3)]
        )
        obj = OfflineDeviceAuthorization.objects.get(id=auth["id"])
        head, sep, tail = obj.signed_token.partition(":")
        obj.signed_token = (
            head[:-2] + ("aa" if head[-2:] != "aa" else "bb") + sep + tail
        )
        obj.save(update_fields=["signed_token"])
        res = login_as(cashier).get(_snapshot_url(auth["id"]))
        assert res.status_code == 400
        assert res.json()["code"] == "invalid_signature"


# --------------------------------------------------------------------------- #
# compatibility with the existing offline sync service                        #
# --------------------------------------------------------------------------- #


class TestSyncCompatibility:
    def test_price_shown_by_the_read_is_the_price_sync_bills(
        self, login_as, branch, owner
    ):
        _made, cashier, auth, _did = _open_session_api(
            login_as, branch, owner, variants=[_v("SYN-1", "1234.00", 20, low=2)]
        )
        cc = login_as(cashier)
        item = cc.get(_snapshot_url(auth["id"])).json()["items"][0]
        assert set(item) == _ITEM_KEYS  # exactly the shape sync's price map keys on

        qty = 3
        cid = str(uuid.uuid4())
        synced = cc.post(
            f"{BASE}/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [
                    {
                        "client_sale_id": cid,
                        "device_sequence": 1,
                        "offline_created_at": timezone.now().isoformat(),
                        "items": [{"variant_id": item["variant_id"], "quantity": qty}],
                        "payments": [
                            {
                                "method": "CASH",
                                "amount": str(Decimal(item["price"]) * qty),
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        assert synced.status_code == 200, synced.content
        [result] = synced.json()["results"]
        assert result["outcome"] == "ACCEPTED"
        sale = Sale.objects.get(client_sale_id=cid)
        assert sale.total == Decimal(item["price"]) * qty


# --------------------------------------------------------------------------- #
# OpenAPI contract + secret hygiene                                           #
# --------------------------------------------------------------------------- #


class TestOpenAPI:
    def test_endpoint_is_documented_with_get_and_a_response_schema(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = "/api/v1/offline/authorizations/{id}/snapshot/"
        assert path in schema["paths"]
        assert "get" in schema["paths"][path]
        ref = str(schema["paths"][path]["get"]["responses"]["200"])
        assert "OfflineCatalogueSnapshot" in ref

    def test_snapshot_components_expose_no_cost_or_secret(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        comps = schema["components"]["schemas"]
        item = comps["OfflineCatalogueSnapshotItem"]["properties"]
        assert set(item) == _ITEM_KEYS
        assert item["price"]["type"] == "string"
        assert item["quantity"]["type"] == "integer"
        blob = str({k: v for k, v in comps.items() if "OfflineCatalogueSnapshot" in k})
        for token in _FORBIDDEN_SUBSTRINGS:
            assert token not in blob

    def test_no_signing_secret_in_the_generated_or_committed_schema(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        assert settings.OFFLINE_SIGNING_KEY not in str(schema)
        text = (ROOT / "openapi.yml").read_text(encoding="utf-8")
        assert "/api/v1/offline/authorizations/{id}/snapshot/" in text
        assert "OfflineCatalogueSnapshot:" in text
        assert settings.OFFLINE_SIGNING_KEY not in text
        assert "OFFLINE_SIGNING_KEY" not in text
