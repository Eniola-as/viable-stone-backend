"""Authentication, MFA, recovery and protected-field-leak tests."""

import pytest

from apps.accounts.services.mfa import start_totp_enrolment
from apps.accounts.tests.mfa_helpers import current_token, latest_device

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"
ME = "/api/v1/auth/me/"

# Fields an employee-facing response must never contain.
FORBIDDEN_FIELDS = {
    "password",
    "is_superuser",
    "user_permissions",
    "average_unit_cost",
    "unit_cost_snapshot",
    "cost",
    "profit",
}


def _error(response):
    body = response.json()
    assert set(body) >= {"code", "message", "field_errors", "request_id"}
    return body


class TestLogin:
    def test_employee_login_success(self, api_client, employee):
        res = api_client.post(
            LOGIN, {"username": "cashier", "password": "cashier-pass-12345"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["mfa_required"] is False
        assert body["user"]["username"] == "cashier"
        assert body["user"]["role"] == "EMPLOYEE"

    def test_wrong_password_returns_401_envelope(self, api_client, employee):
        res = api_client.post(LOGIN, {"username": "cashier", "password": "nope"})
        assert res.status_code == 401
        assert _error(res)["code"] == "invalid_credentials"

    def test_inactive_account_rejected(self, api_client, employee):
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        res = api_client.post(
            LOGIN, {"username": "cashier", "password": "cashier-pass-12345"}
        )
        assert res.status_code == 403
        assert _error(res)["code"] == "account_disabled"

    def test_login_requires_csrf_token(self, csrf_client, employee):
        res = csrf_client.post(
            LOGIN, {"username": "cashier", "password": "cashier-pass-12345"}
        )
        assert res.status_code == 403
        assert _error(res)["code"] == "csrf_failed"

    def test_login_with_csrf_token_succeeds(self, csrf_client, employee):
        # Prime the CSRF cookie via the bootstrap endpoint, then send the header.
        csrf_client.get("/api/v1/auth/csrf/")
        token = csrf_client.cookies["vs_csrftoken"].value
        res = csrf_client.post(
            LOGIN,
            {"username": "cashier", "password": "cashier-pass-12345"},
            HTTP_X_CSRFTOKEN=token,
        )
        assert res.status_code == 200


class TestCurrentUser:
    def test_requires_authentication(self, api_client):
        res = api_client.get(ME)
        assert res.status_code == 401
        assert _error(res)["code"] == "not_authenticated"

    def test_returns_safe_profile_only(self, login_as, employee):
        client = login_as(employee)
        res = client.get(ME)
        assert res.status_code == 200
        body = res.json()
        assert FORBIDDEN_FIELDS.isdisjoint(body.keys())

    def test_request_id_header_present(self, api_client):
        res = api_client.get("/api/v1/health/")
        assert res.headers.get("X-Request-ID")


class TestOwnerMFA:
    def test_owner_login_withholds_profile_until_mfa(self, api_client, owner):
        res = api_client.post(
            LOGIN, {"username": "owner", "password": "owner-pass-12345"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["mfa_required"] is True
        assert body["user"] is None

    def test_owner_business_endpoint_blocked_until_mfa_cleared(self, login_as, owner):
        client = login_as(owner, mfa_verified=False)
        assert client.get(ME).status_code == 403

    def test_full_enrolment_flow_yields_recovery_codes(self, login_as, owner):
        client = login_as(owner, mfa_verified=False)
        setup = client.post("/api/v1/auth/mfa/setup/")
        assert setup.status_code == 200
        assert setup.json()["secret"]

        device = latest_device(owner, confirmed=False)
        confirm = client.post(
            "/api/v1/auth/mfa/setup/confirm/", {"token": current_token(device)}
        )
        assert confirm.status_code == 200
        codes = confirm.json()["recovery_codes"]
        assert len(codes) == 10
        # MFA now cleared for the session.
        assert client.get(ME).status_code == 200

    def test_enrolled_owner_verifies_with_totp(self, login_as, owner):
        # Enrol once.
        start_totp_enrolment(owner)
        device = latest_device(owner, confirmed=False)
        device.confirmed = True
        device.save(update_fields=["confirmed"])

        client = login_as(owner, mfa_verified=False)
        bad = client.post("/api/v1/auth/mfa/verify/", {"token": "000000"})
        assert bad.status_code == 400
        assert _error(bad)["code"] == "mfa_invalid_token"

        ok = client.post("/api/v1/auth/mfa/verify/", {"token": current_token(device)})
        assert ok.status_code == 200
        assert client.get(ME).status_code == 200

    def test_recovery_code_is_single_use(self, login_as, owner):
        client = login_as(owner, mfa_verified=False)
        client.post("/api/v1/auth/mfa/setup/")
        device = latest_device(owner, confirmed=False)
        codes = client.post(
            "/api/v1/auth/mfa/setup/confirm/", {"token": current_token(device)}
        ).json()["recovery_codes"]

        fresh = login_as(owner, mfa_verified=False)
        first = fresh.post("/api/v1/auth/mfa/recovery/", {"code": codes[0]})
        assert first.status_code == 200

        again = login_as(owner, mfa_verified=False)
        reused = again.post("/api/v1/auth/mfa/recovery/", {"code": codes[0]})
        assert reused.status_code == 400
        assert _error(reused)["code"] == "recovery_code_invalid"


class TestPasswordChange:
    def test_change_clears_must_change_password(self, login_as, employee):
        assert employee.must_change_password is True
        client = login_as(employee)
        res = client.post(
            "/api/v1/auth/password/change/",
            {
                "current_password": "cashier-pass-12345",
                "new_password": "brand-new-pass-98765",
            },
        )
        assert res.status_code == 200
        employee.refresh_from_db()
        assert employee.must_change_password is False

    def test_wrong_current_password_rejected(self, login_as, employee):
        client = login_as(employee)
        res = client.post(
            "/api/v1/auth/password/change/",
            {"current_password": "wrong", "new_password": "brand-new-pass-98765"},
        )
        assert res.status_code == 400
        assert "current_password" in _error(res)["field_errors"]
