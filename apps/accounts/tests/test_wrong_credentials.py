"""Wrong credentials must never yield an authenticated, MFA-cleared session.

Covers the exact sequence a manual tester reported — wrong username, wrong
password, wrong authenticator code, wrong recovery code — and the mechanism
behind "I was able to log in anyway": a **pre-existing** browser session that a
failed sign-in attempt used to leave untouched.

django-axes is enabled here (as in development / production); its failure limit
is raised so these correctness tests do not trip the lockout and stay
order-independent.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.accounts.tests.mfa_helpers import current_token, latest_device

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"
ME = "/api/v1/auth/me/"
MFA_VERIFY = "/api/v1/auth/mfa/verify/"
MFA_RECOVERY = "/api/v1/auth/mfa/recovery/"
PRODUCTS = "/api/v1/products/"


@pytest.fixture(autouse=True)
def _axes_on_no_lockout(settings):
    settings.AXES_ENABLED = True
    settings.AXES_FAILURE_LIMIT = 1000
    cache.clear()
    yield
    cache.clear()


def _login_owner(client):
    res = client.post(LOGIN, {"username": "owner", "password": "owner-pass-12345"})
    assert res.status_code == 200, res.content
    body = res.json()
    assert body == {
        "mfa_required": True,
        "mfa_enrolled": body["mfa_enrolled"],
        "mfa_verified": False,
        "must_change_password": body["must_change_password"],
        "user": None,
    }
    return client


class TestWrongPasswordCannotAuthenticate:
    def test_wrong_password_is_401_and_no_session(self, api_client, employee):
        res = api_client.post(LOGIN, {"username": "cashier", "password": "WRONG-pw-1"})
        assert res.status_code == 401
        assert res.json()["code"] == "invalid_credentials"
        assert api_client.get(ME).status_code == 401
        assert api_client.get(PRODUCTS).status_code == 401

    def test_unknown_username_is_401(self, api_client, db):
        res = api_client.post(LOGIN, {"username": "ghost", "password": "whatever123"})
        assert res.status_code == 401
        assert api_client.get(ME).status_code == 401

    def test_blank_password_is_400_and_no_session(self, api_client, employee):
        assert (
            api_client.post(LOGIN, {"username": "cashier", "password": ""}).status_code
            == 400
        )
        assert api_client.get(ME).status_code == 401


class TestWrongMfaCannotClearSession:
    def test_owner_with_right_password_is_blocked_until_mfa(self, api_client, owner):
        client = _login_owner(api_client)
        assert client.get(ME).status_code == 403
        assert client.get(PRODUCTS).status_code == 403

    def test_wrong_totp_code_does_not_clear_mfa(self, api_client, owner):
        from apps.accounts.services.mfa import start_totp_enrolment

        start_totp_enrolment(owner)
        device = latest_device(owner, confirmed=False)
        device.confirmed = True
        device.save(update_fields=["confirmed"])

        client = _login_owner(api_client)
        bad = client.post(MFA_VERIFY, {"token": "000000"})
        assert bad.status_code == 400
        assert bad.json()["code"] == "mfa_invalid_token"
        assert client.get(ME).status_code == 403
        assert client.get(PRODUCTS).status_code == 403

    def test_totp_verify_without_a_device_is_rejected(self, api_client, owner):
        client = _login_owner(api_client)
        assert client.post(MFA_VERIFY, {"token": "123456"}).status_code == 400
        assert client.get(ME).status_code == 403

    def test_wrong_recovery_code_does_not_clear_mfa(self, api_client, owner):
        from apps.accounts.services.recovery import generate_recovery_codes

        generate_recovery_codes(owner)  # real codes exist; we send a wrong one
        client = _login_owner(api_client)
        res = client.post(MFA_RECOVERY, {"code": "aaaa-bbbb-cccc"})
        assert res.status_code == 400
        assert res.json()["code"] == "recovery_code_invalid"
        assert client.get(ME).status_code == 403

    def test_recovery_without_any_codes_is_rejected(self, api_client, owner):
        client = _login_owner(api_client)
        assert client.post(MFA_RECOVERY, {"code": "aaaa-bbbb-cccc"}).status_code == 400
        assert client.get(ME).status_code == 403

    def test_full_wrong_sequence_never_grants_access(self, api_client, owner):
        from apps.accounts.services.mfa import start_totp_enrolment
        from apps.accounts.services.recovery import generate_recovery_codes

        start_totp_enrolment(owner)
        device = latest_device(owner, confirmed=False)
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        generate_recovery_codes(owner)

        assert (
            api_client.post(
                LOGIN, {"username": "not-owner", "password": "bad-pass-1"}
            ).status_code
            == 401
        )
        client = _login_owner(api_client)
        assert client.post(MFA_VERIFY, {"token": "999999"}).status_code == 400
        assert client.post(MFA_RECOVERY, {"code": "zzzz-zzzz-zzzz"}).status_code == 400
        assert client.get(ME).status_code == 403
        assert client.get(PRODUCTS).status_code == 403

        # the RIGHT code still works — proving the gate is real, not just closed
        assert (
            client.post(MFA_VERIFY, {"token": current_token(device)}).status_code == 200
        )
        assert client.get(ME).status_code == 200


class TestFailedLoginDropsAnExistingSession:
    """The reported "I logged in with wrong credentials" — really a stale,
    still-valid session that a failed sign-in attempt no longer preserves."""

    def test_failed_relogin_invalidates_a_prior_session(self, login_as, employee):
        client = login_as(employee)  # a real, working session
        assert client.get(ME).status_code == 200

        bad = client.post(LOGIN, {"username": "cashier", "password": "NOT-the-pw"})
        assert bad.status_code == 401
        # the earlier session must no longer authenticate anything
        assert client.get(ME).status_code == 401
        assert client.get(PRODUCTS).status_code == 401

    def test_failed_relogin_by_an_mfa_cleared_owner_drops_access(
        self, api_client, owner
    ):
        from apps.accounts.services.mfa import start_totp_enrolment

        start_totp_enrolment(owner)
        device = latest_device(owner, confirmed=False)
        device.confirmed = True
        device.save(update_fields=["confirmed"])

        client = _login_owner(api_client)
        client.post(MFA_VERIFY, {"token": current_token(device)})
        assert client.get(ME).status_code == 200  # fully in

        assert (
            client.post(
                LOGIN, {"username": "owner", "password": "WRONG-now"}
            ).status_code
            == 401
        )
        assert client.get(ME).status_code == 401  # MFA-cleared session gone too

    def test_a_400_login_body_keeps_the_existing_session(self, login_as, employee):
        client = login_as(employee)
        assert client.post(LOGIN, {"username": "cashier"}).status_code == 400
        # a malformed request is not a credential attempt — session survives
        assert client.get(ME).status_code == 200

    def test_successful_relogin_as_another_user_switches_cleanly(
        self, login_as, employee, owner
    ):
        client = login_as(employee)
        assert client.get(ME).json()["username"] == "cashier"
        res = client.post(LOGIN, {"username": "owner", "password": "owner-pass-12345"})
        assert res.status_code == 200
        assert res.json()["mfa_required"] is True
        # now an unverified owner session — employee access is gone
        assert client.get(ME).status_code == 403
