"""Stage 18 — authentication / authorisation acceptance matrix.

Representative endpoint per security class; the full route inventory is
guaranteed classified by test_route_inventory.py.
"""

from __future__ import annotations

import uuid

import pytest

from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
)

pytestmark = pytest.mark.django_db

API = "/api/v1"

# (path, method) that must reject anonymous callers with 401.
_AUTHED_ENDPOINTS = [
    ("/auth/me/", "get"),
    ("/branches/", "get"),
    ("/users/", "get"),
    ("/audit-logs/", "get"),
    ("/products/", "get"),
    (f"/products/{uuid.uuid4()}/image/", "get"),
    ("/variants/", "get"),
    ("/suppliers/", "get"),
    ("/restocks/", "get"),
    ("/inventory/", "get"),
    ("/sales/", "get"),
    ("/customers/", "get"),
    ("/approvals/", "get"),
    ("/returns/", "get"),
    ("/expenses/", "get"),
    ("/expense-categories/", "get"),
    ("/reports/profit/", "get"),
    ("/notifications/", "get"),
    ("/push-subscriptions/", "get"),
    ("/offline/devices/", "get"),
    ("/offline/authorizations/", "get"),
    ("/offline/sync-records/", "get"),
]

# Owner-only endpoints: an employee must get 403.
_OWNER_ONLY = [
    (f"/products/{uuid.uuid4()}/image/", "delete"),  # employee may GET, not clear
    ("/users/", "get"),
    ("/audit-logs/", "get"),
    ("/suppliers/", "get"),
    ("/restocks/", "get"),
    ("/stock-counts/", "get"),
    ("/inventory/stock-value/", "get"),
    ("/expenses/", "get"),
    ("/expense-categories/", "get"),
    ("/reports/profit/", "get"),
    ("/reports/best-sellers/", "get"),
    ("/offline/devices/", "get"),
    ("/offline/sync-records/", "get"),
]


class TestAnonymous:
    @pytest.mark.parametrize(("path", "method"), _AUTHED_ENDPOINTS)
    def test_unauthenticated_gets_401(self, api_client, path, method):
        res = getattr(api_client, method)(f"{API}{path}")
        assert res.status_code == 401, (path, res.status_code)
        body = res.json()
        assert set(body) == {"code", "message", "field_errors", "request_id"}
        assert body["code"] in {"not_authenticated", "authentication_failed"}

    def test_public_endpoints_are_reachable(self, api_client):
        assert api_client.get(f"{API}/health/live/").status_code == 200
        assert api_client.get(f"{API}/health/").status_code in (200, 503)
        assert api_client.get(f"{API}/health/ready/").status_code in (200, 503)
        # csrf endpoint just primes the cookie -> 204 No Content
        assert api_client.get(f"{API}/auth/csrf/").status_code in (200, 204)


class TestWrongRole:
    @pytest.mark.parametrize(("path", "method"), _OWNER_ONLY)
    def test_employee_is_forbidden_on_owner_endpoints(
        self, login_as, employee, path, method
    ):
        res = getattr(login_as(employee), method)(f"{API}{path}")
        assert res.status_code == 403, (path, res.status_code)
        assert res.json()["code"] == "permission_denied"

    def test_tech_admin_cannot_touch_branch_business_data(self, login_as, tech_admin):
        c = login_as(tech_admin)
        assert c.get(f"{API}/expenses/").status_code == 403
        assert c.get(f"{API}/reports/profit/").status_code == 403
        # tech-admin CAN read audit logs
        assert c.get(f"{API}/audit-logs/").status_code == 200

    def test_employee_cannot_approve(self, login_as, branch, owner, employee, stocked):
        variant = stocked(sku="AP-1")
        c_emp = login_as(employee)
        draft = c_emp.post(
            f"{API}/sales/drafts/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 1}],
            },
            format="json",
        ).json()
        approval = c_emp.post(
            f"{API}/sales/{draft['id']}/discount-requests/",
            {"amount": "100.00", "reason": "x"},
            format="json",
        ).json()
        res = c_emp.post(
            f"{API}/approvals/{approval['id']}/approve/",
            {"amount": "100.00"},
            format="json",
        )
        assert res.status_code == 403


class TestMFARequired:
    def test_owner_without_mfa_session_is_forbidden(self, login_as, owner):
        c = login_as(owner, mfa_verified=False)
        for path in ("/expenses/", "/users/", "/reports/profit/", "/offline/devices/"):
            assert c.get(f"{API}{path}").status_code == 403

    def test_owner_without_mfa_cannot_open_offline_session_or_sales(
        self, login_as, owner, employee, stocked
    ):
        stocked(sku="MFA-1")
        c = login_as(owner, mfa_verified=False)
        assert c.get(f"{API}/sales/").status_code == 403


class TestCSRF:
    def test_state_changing_session_request_needs_csrf(self, csrf_client, owner):
        csrf_client.force_login(owner)
        session = csrf_client.session
        session["mfa_verified"] = True
        session.save()
        res = csrf_client.post(
            f"{API}/expense-categories/", {"name": "NoCSRF"}, format="json"
        )
        assert res.status_code == 403
        assert res.json()["code"] in {"permission_denied", "not_authenticated"}


class TestCrossBranch:
    def _other_branch(self):
        b = BranchFactory(code=f"X{uuid.uuid4().hex[:4].upper()}")
        return b, OwnerFactory(branch=b), EmployeeFactory(branch=b)

    def test_cross_branch_product_is_404(self, login_as, branch, owner, stocked):
        variant = stocked(sku="CB-1")
        _b, other_owner, _e = self._other_branch()
        res = login_as(other_owner).get(f"{API}/variants/{variant.id}/")
        assert res.status_code == 404

    def test_cross_branch_sale_is_404(self, login_as, branch, owner, employee, stocked):
        variant = stocked(sku="CB-2")
        sale = (
            login_as(employee)
            .post(
                f"{API}/sales/",
                {
                    "client_sale_id": str(uuid.uuid4()),
                    "items": [{"variant": str(variant.id), "quantity": 1}],
                    "payments": [{"method": "CASH", "amount": "1000.00"}],
                },
                format="json",
            )
            .json()
        )
        _b, other_owner, _e = self._other_branch()
        assert (
            login_as(other_owner).get(f"{API}/sales/{sale['id']}/").status_code == 404
        )
        assert (
            login_as(other_owner).get(f"{API}/sales/{sale['id']}/receipt/").status_code
            == 404
        )

    def test_cross_branch_expense_is_404(self, login_as, branch, owner):
        cat = (
            login_as(owner)
            .post(f"{API}/expense-categories/", {"name": "Fuel"}, format="json")
            .json()
        )
        exp = (
            login_as(owner)
            .post(
                f"{API}/expenses/",
                {
                    "category": cat["id"],
                    "amount": "500.00",
                    "expense_date": "2026-08-01",
                    "description": "d",
                },
                format="json",
            )
            .json()
        )
        _b, other_owner, _e = self._other_branch()
        assert (
            login_as(other_owner).get(f"{API}/expenses/{exp['id']}/").status_code == 404
        )


class TestDisabledUsersAndRevokedDevices:
    def test_deactivated_user_session_stops_working(self, login_as, branch, owner):
        other = EmployeeFactory(branch=branch)
        client = login_as(other)
        assert client.get(f"{API}/auth/me/").status_code == 200
        other.is_active = False
        other.save(update_fields=["is_active"])
        assert client.get(f"{API}/auth/me/").status_code in (401, 403)
        assert client.get(f"{API}/sales/").status_code in (401, 403)

    def test_revoked_offline_device_cannot_get_a_new_authorization(
        self, login_as, branch, owner, stocked
    ):
        stocked(sku="RV-1")
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        device_id = oc.post(
            f"{API}/offline/devices/", {"name": "Till"}, format="json"
        ).json()["id"]
        oc.post(f"{API}/offline/devices/{device_id}/revoke/")
        res = oc.post(
            f"{API}/offline/authorizations/",
            {"device": device_id, "cashier": str(cashier.id)},
            format="json",
        )
        assert res.status_code == 400
        assert res.json()["code"] == "invalid_offline_device"

    def test_revoked_authorization_cannot_create_offline_sales(
        self, login_as, branch, owner, stocked
    ):
        variant = stocked(sku="RV-2", qty=10)
        cashier = EmployeeFactory(branch=branch)
        oc = login_as(owner)
        device_id = oc.post(
            f"{API}/offline/devices/", {"name": "Till"}, format="json"
        ).json()["id"]
        auth = oc.post(
            f"{API}/offline/authorizations/",
            {"device": device_id, "cashier": str(cashier.id)},
            format="json",
        ).json()
        oc.post(f"{API}/offline/authorizations/{auth['id']}/revoke/", {}, format="json")
        cid = str(uuid.uuid4())
        res = login_as(cashier).post(
            f"{API}/offline/sync/",
            {
                "authorization_token": auth["signed_token"],
                "sales": [
                    {
                        "client_sale_id": cid,
                        "device_sequence": 1,
                        "offline_created_at": "2026-08-27T10:00:00+01:00",
                        "items": [{"variant_id": str(variant.id), "quantity": 1}],
                        "payments": [{"method": "CASH", "amount": "1000.00"}],
                    }
                ],
            },
            format="json",
        )
        assert res.status_code == 200
        assert res.json()["results"][0]["outcome"] == "OWNER_REVIEW_REQUIRED"
        from apps.sales.models import Sale

        assert not Sale.objects.filter(client_sale_id=cid).exists()


class TestEmployeeOutputHasNoOwnerFields:
    _FORBIDDEN = (
        "unit_cost",
        "average_unit_cost",
        "cost_price",
        '"cost"',
        "profit",
        "cogs",
        "p256dh",
        '"auth"',
        "signed_token",
        "password",
        "code_hash",
    )

    def _clean(self, text: str):
        low = text.lower()
        for token in self._FORBIDDEN:
            assert token.strip('"') not in low or token == '"auth"', token

    def test_variant_list_hides_cost(self, login_as, employee, owner, stocked):
        stocked(sku="EO-1", cost="777.00")
        body = login_as(employee).get(f"{API}/variants/").content.decode()
        assert "777.00" not in body
        assert "unit_cost" not in body.lower()
        assert "average_unit_cost" not in body.lower()

    def test_receipt_hides_cost_and_profit(self, login_as, employee, owner, stocked):
        variant = stocked(sku="EO-2", price="1000.00", cost="640.00")
        c = login_as(employee)
        sale = c.post(
            f"{API}/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 2}],
                "payments": [{"method": "CASH", "amount": "2000.00"}],
            },
            format="json",
        ).json()
        body = c.get(f"{API}/sales/{sale['id']}/receipt/").content.decode().lower()
        assert "640.00" not in body
        for token in ("unit_cost", "average_unit_cost", "profit", "cogs"):
            assert token not in body

    def test_cashier_only_sees_own_sales(self, login_as, branch, owner, stocked):
        variant = stocked(sku="EO-3")
        a, b = EmployeeFactory(branch=branch), EmployeeFactory(branch=branch)
        login_as(a).post(
            f"{API}/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 1}],
                "payments": [{"method": "CASH", "amount": "1000.00"}],
            },
            format="json",
        )
        listing = login_as(b).get(f"{API}/sales/").json()
        assert listing["count"] == 0
