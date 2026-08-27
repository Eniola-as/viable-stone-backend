"""Stage 17 — Sentry is inert without a DSN and scrubs sensitive data."""

from apps.core import sentry
from apps.core.request_context import set_request_id


class _Env:
    def __init__(self, values):
        self.values = values

    def __call__(self, key, default=None):
        return self.values.get(key, default)

    def float(self, key, default=0.0):
        return float(self.values.get(key, default))


class TestBuildKwargs:
    def test_no_dsn_means_no_sentry(self):
        assert sentry.build_sentry_kwargs(_Env({})) is None
        assert sentry.configure_sentry(_Env({})) is False

    def test_dsn_present_yields_safe_defaults(self):
        kwargs = sentry.build_sentry_kwargs(
            _Env(
                {
                    "SENTRY_DSN": "https://k@o.ingest.sentry.io/1",
                    "SENTRY_TRACES_SAMPLE_RATE": "0.05",
                    "SENTRY_ENVIRONMENT": "production",
                }
            )
        )
        assert kwargs["send_default_pii"] is False
        assert kwargs["max_request_body_size"] == "never"
        assert kwargs["traces_sample_rate"] == 0.05
        assert kwargs["before_send"] is sentry.scrub_event


class TestScrubEvent:
    def test_strips_cookies_auth_headers_and_request_body(self):
        event = {
            "request": {
                "headers": {
                    "Cookie": "vs_sessionid=abc",
                    "Authorization": "Bearer secret",
                    "X-CSRFToken": "tok",
                    "User-Agent": "Firefox",
                },
                "cookies": {"vs_sessionid": "abc"},
                "data": {"password": "hunter2", "cart": [1, 2]},
                "query_string": "token=leak",
            },
        }
        cleaned = sentry.scrub_event(event)
        headers = cleaned["request"]["headers"]
        assert headers["Cookie"] == "[scrubbed]"
        assert headers["Authorization"] == "[scrubbed]"
        assert headers["X-CSRFToken"] == "[scrubbed]"
        assert headers["User-Agent"] == "Firefox"
        assert "cookies" not in cleaned["request"]
        assert "data" not in cleaned["request"]
        assert "query_string" not in cleaned["request"]

    def test_scrubs_sensitive_extra_keys_and_keeps_request_id(self):
        set_request_id("req-123")
        event = {
            "extra": {
                "customer_phone": "08011112222",
                "payment_reference": "TRF-9",
                "signed_token": "eyJ...",
                "note": "ok to keep",
            }
        }
        cleaned = sentry.scrub_event(event)
        assert cleaned["extra"]["customer_phone"] == "[scrubbed]"
        assert cleaned["extra"]["payment_reference"] == "[scrubbed]"
        assert cleaned["extra"]["signed_token"] == "[scrubbed]"
        assert cleaned["extra"]["note"] == "ok to keep"
        assert cleaned["tags"]["request_id"] == "req-123"

    def test_reduces_user_to_id_and_username(self):
        cleaned = sentry.scrub_event(
            {"user": {"id": "1", "username": "owner", "email": "o@x.com"}}
        )
        assert cleaned["user"] == {"id": "1", "username": "owner"}
