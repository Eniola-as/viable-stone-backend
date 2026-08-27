"""Stage 18 — safe error envelope + structured security logging (spec item 6).

Every error category returns the standard envelope with a request id and leaks
no internals; every security-relevant rejection writes a scrubbed
``apps.security`` line that keeps the coordinates for an investigation but never
a password, token, payment reference, request body or customer phone number.
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

API = "/api/v1"
_ENVELOPE_KEYS = {"code", "message", "field_errors", "request_id"}

# Substrings that must never appear in any error response body.
_FORBIDDEN_IN_BODY = (
    "traceback",
    '  file "',
    "site-packages",
    "/srv/",
    "c:\\users",
    "select ",
    "insert into",
    "psycopg",
    "postgresql://",
    "redis://",
    "secret_key",
    "django_settings_module",
    "runtimeerror",
    "boom-internal",
)


def _bad_login(client, username="nobody", password="wrong-pass-abc"):
    return client.post(
        f"{API}/auth/login/", {"username": username, "password": password}
    )


class TestErrorEnvelopeForEveryCategory:
    def test_malformed_json_is_parse_error(self, login_as, owner):
        res = login_as(owner).post(
            f"{API}/expense-categories/",
            data="{not valid json",
            content_type="application/json",
        )
        assert res.status_code == 400
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "parse_error"

    def test_validation_error_carries_field_errors(self, login_as, owner):
        res = login_as(owner).post(f"{API}/expense-categories/", {}, format="json")
        assert res.status_code == 400
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "validation_error"
        assert "name" in body["field_errors"]

    def test_unauthenticated_is_401(self, api_client):
        res = api_client.get(f"{API}/expenses/")
        assert res.status_code == 401
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] in {"not_authenticated", "authentication_failed"}

    def test_bad_credentials_is_401(self, api_client):
        res = _bad_login(api_client)
        assert res.status_code == 401
        assert set(res.json()) == _ENVELOPE_KEYS
        assert res.json()["code"] == "invalid_credentials"

    def test_permission_denied_is_403(self, login_as, employee):
        res = login_as(employee).get(f"{API}/expenses/")
        assert res.status_code == 403
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "permission_denied"

    def test_not_found_and_cross_branch_are_404_not_found(self, login_as, owner):
        # A missing id and another branch's id are indistinguishable by design.
        res = login_as(owner).get(f"{API}/expenses/{uuid.uuid4()}/")
        assert res.status_code == 404
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "not_found"

    def test_idempotency_conflict_is_409(self, login_as, employee, stocked):
        variant = stocked(sku="SE-1")
        c = login_as(employee)
        cid = str(uuid.uuid4())
        draft = c.post(
            f"{API}/sales/drafts/",
            {
                "client_sale_id": cid,
                "items": [{"variant": str(variant.id), "quantity": 1}],
            },
            format="json",
        )
        assert draft.status_code == 201, draft.content
        res = c.post(
            f"{API}/sales/",
            {
                "client_sale_id": cid,
                "items": [{"variant": str(variant.id), "quantity": 1}],
                "payments": [{"method": "CASH", "amount": "1000.00"}],
            },
            format="json",
        )
        assert res.status_code == 409
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "sale_in_progress"

    def test_rate_limit_is_429_with_envelope_and_retry_after(
        self, api_client, monkeypatch
    ):
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**SimpleRateThrottle.THROTTLE_RATES, "auth_login": "1/min"},
        )
        seen = [_bad_login(api_client).status_code for _ in range(3)]
        assert 429 in seen
        throttled = _bad_login(api_client)
        assert throttled.status_code == 429
        assert set(throttled.json()) == _ENVELOPE_KEYS
        assert throttled.json()["code"] == "throttled"
        assert throttled.headers.get("Retry-After")

    def test_unhandled_server_error_is_a_generic_500(self, owner, monkeypatch):
        def _explode(*a, **kw):
            raise RuntimeError(
                "boom-internal SELECT * FROM sales_sale; /srv/app/secrets.py"
            )

        monkeypatch.setattr(
            "apps.finance.api.views.profit_report", _explode, raising=True
        )
        client = APIClient()
        client.raise_request_exception = False
        client.force_login(owner)
        session = client.session
        session["mfa_verified"] = True
        session.save()

        res = client.get(f"{API}/reports/profit/")
        assert res.status_code == 500
        body = res.json()
        assert set(body) == _ENVELOPE_KEYS
        assert body["code"] == "server_error"
        assert body["request_id"]
        blob = res.content.decode().lower()
        assert not any(tok in blob for tok in _FORBIDDEN_IN_BODY), blob


class TestNoResponseLeaksInternals:
    @pytest.mark.parametrize(
        "make",
        [
            lambda c: c.get(f"{API}/expenses/{uuid.uuid4()}/"),
            lambda c: c.post(
                f"{API}/expense-categories/",
                data="{bad",
                content_type="application/json",
            ),
            lambda c: c.get(f"{API}/nope/does/not/exist/"),
            lambda c: c.post(f"{API}/expense-categories/", {}, format="json"),
        ],
    )
    def test_error_bodies_are_clean(self, login_as, owner, make):
        res = make(login_as(owner))
        blob = res.content.decode().lower()
        assert not any(tok in blob for tok in _FORBIDDEN_IN_BODY), (
            res.status_code,
            blob,
        )


def _security_records(caplog):
    return [r for r in caplog.records if r.name == "apps.security"]


def _flatten(record) -> str:
    return " ".join(f"{k}={v}" for k, v in record.__dict__.items()).lower()


class TestStructuredSecurityLogging:
    def test_repeated_auth_failure_is_logged_without_the_password(
        self, api_client, caplog
    ):
        pw = "corr3ct-h0rse-batt3ry"
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            for _ in range(3):
                _bad_login(api_client, username="mallory", password=pw)
        recs = _security_records(caplog)
        assert len(recs) >= 3
        rec = recs[-1]
        assert rec.getMessage() == "security_event"
        assert getattr(rec, "event", None) == "auth_failed"
        assert getattr(rec, "code", None) == "invalid_credentials"
        assert "mallory" in _flatten(rec)  # username kept for investigation
        assert pw not in _flatten(rec)
        assert "password" not in _flatten(rec)

    def test_axes_lockout_response_logs_a_scrubbed_event(self, rf, caplog):
        from apps.accounts.auth import lockout_response

        request = rf.post(f"{API}/auth/login/")
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            response = lockout_response(
                request, credentials={"username": "victim", "password": "PLAINTEXT-PW"}
            )
        assert response.status_code == 429
        payload = json.loads(response.content)
        assert set(payload) == _ENVELOPE_KEYS
        assert payload["code"] == "too_many_attempts"
        rec = _security_records(caplog)[-1]
        assert getattr(rec, "event", None) == "axes_lockout"
        assert getattr(rec, "code", None) == "too_many_attempts"
        assert "victim" in _flatten(rec)
        assert "plaintext-pw" not in _flatten(rec)
        assert "password" not in _flatten(rec)

    def test_permission_denied_is_logged(self, login_as, employee, caplog):
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            login_as(employee).get(f"{API}/reports/profit/")
        events = {getattr(r, "event", None) for r in _security_records(caplog)}
        assert "permission_denied" in events

    def test_invalid_offline_signature_is_logged(self, login_as, employee, caplog):
        payload = {
            "authorization_token": "not-a-real-signed-token.deadbeef",
            "sales": [
                {
                    "client_sale_id": str(uuid.uuid4()),
                    "device_sequence": 1,
                    "offline_created_at": "2026-08-27T10:00:00+01:00",
                    "items": [{"variant_id": str(uuid.uuid4()), "quantity": 1}],
                    "payments": [{"method": "CASH", "amount": "1000.00"}],
                }
            ],
        }
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            res = login_as(employee).post(
                f"{API}/offline/sync/", payload, format="json"
            )
        assert res.status_code == 400
        assert res.json()["code"] == "invalid_signature"
        events = {getattr(r, "event", None) for r in _security_records(caplog)}
        assert "invalid_offline_signature" in events

    def test_rejected_upload_is_logged(self, login_as, owner, caplog):
        cat = (
            login_as(owner)
            .post(f"{API}/expense-categories/", {"name": "Fuel"}, format="json")
            .json()
        )
        upload = io.BytesIO(b"MZ\x90\x00 this is actually an executable")
        upload.name = "invoice.pdf"
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            res = login_as(owner).post(
                f"{API}/expenses/",
                {
                    "category": cat["id"],
                    "amount": "10.00",
                    "expense_date": "2026-08-01",
                    "description": "d",
                    "receipt_file": upload,
                },
                format="multipart",
            )
        assert res.status_code == 400
        events = {getattr(r, "event", None) for r in _security_records(caplog)}
        assert "rejected_upload" in events

    def test_unexpected_server_error_is_logged_without_internals(
        self, owner, monkeypatch, caplog
    ):
        def _explode(*a, **kw):
            raise RuntimeError("boom-internal secret_key=abc123 /srv/app/x.py")

        monkeypatch.setattr(
            "apps.finance.api.views.profit_report", _explode, raising=True
        )
        client = APIClient()
        client.raise_request_exception = False
        client.force_login(owner)
        session = client.session
        session["mfa_verified"] = True
        session.save()

        with caplog.at_level(logging.ERROR, logger="apps.security"):
            client.get(f"{API}/reports/profit/")
        rec = _security_records(caplog)[-1]
        assert getattr(rec, "event", None) == "server_error"
        assert getattr(rec, "status", None) == 500
        flat = _flatten(rec)
        assert "boom-internal" not in flat
        assert "secret_key" not in flat


class TestSentryScrubbingStillWorks:
    def test_scrub_event_strips_body_headers_and_sensitive_keys(self):
        from apps.core.sentry import scrub_event

        event = {
            "request": {
                "headers": {
                    "Cookie": "vs_sessionid=abc",
                    "Authorization": "Bearer xyz",
                    "X-CSRFToken": "tok",
                    "User-Agent": "pytest",
                },
                "cookies": {"vs_sessionid": "abc"},
                "data": {"password": "hunter2", "pan": "4111111111111111"},
                "query_string": "password=hunter2",
            },
            "extra": {
                "signed_token": "s.s.s",
                "customer_phone": "08030000000",
                "payment_reference": "PMT-1",
                "note": "keep me",
            },
            "user": {"id": "1", "username": "x", "ip_address": "1.2.3.4"},
        }
        out = scrub_event(dict(event))
        req = out["request"]
        assert req["headers"]["Cookie"] == "[scrubbed]"
        assert req["headers"]["Authorization"] == "[scrubbed]"
        assert req["headers"]["User-Agent"] == "pytest"
        assert "cookies" not in req and "data" not in req
        assert "query_string" not in req
        assert out["extra"]["signed_token"] == "[scrubbed]"
        assert out["extra"]["customer_phone"] == "[scrubbed]"
        assert out["extra"]["payment_reference"] == "[scrubbed]"
        assert out["extra"]["note"] == "keep me"
        assert set(out["user"]) == {"id", "username"}
        assert out["tags"]["request_id"]

    def test_before_send_is_wired_when_a_dsn_is_configured(self, monkeypatch):
        import environ

        from apps.core.sentry import build_sentry_kwargs, scrub_event

        monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        kwargs = build_sentry_kwargs(environ.Env())
        assert kwargs is not None
        assert kwargs["before_send"] is scrub_event
        assert kwargs["before_send_transaction"] is scrub_event
        assert kwargs["send_default_pii"] is False
        assert kwargs["max_request_body_size"] == "never"
