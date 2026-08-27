"""Stage 18 — rate-limiting acceptance."""

from __future__ import annotations

import logging

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db

API = "/api/v1"


class TestThrottleConfiguration:
    def test_global_baseline_for_anon_and_authenticated_traffic(self):
        classes = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]
        assert "rest_framework.throttling.AnonRateThrottle" in classes
        assert "rest_framework.throttling.UserRateThrottle" in classes
        assert "rest_framework.throttling.ScopedRateThrottle" in classes
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        assert rates["anon"] and rates["user"]

    def test_sensitive_scopes_are_defined(self):
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        for scope in (
            "auth_login",
            "auth_mfa",
            "auth_recovery",
            "sales_write",
            "notifications_write",
            "offline_sync",
        ):
            assert rates.get(scope)

    @pytest.mark.parametrize(
        ("module", "attr", "scope"),
        [
            ("apps.accounts.api.auth_views", "LoginView", "auth_login"),
            ("apps.accounts.api.auth_views", "MFAVerifyView", "auth_mfa"),
            ("apps.accounts.api.auth_views", "RecoveryCodeVerifyView", "auth_recovery"),
            ("apps.sales.api.views", "SaleViewSet", "sales_write"),
            ("apps.sales.api.offline_views", "OfflineSyncView", "offline_sync"),
        ],
    )
    def test_named_views_keep_their_scope(self, module, attr, scope):
        view = getattr(__import__(module, fromlist=[attr]), attr)
        assert getattr(view, "throttle_scope", None) == scope, (
            f"{attr} lost its '{scope}' throttle scope"
        )

    def test_conditional_scopes_are_attached(self):
        from apps.notifications.api.views import PushSubscriptionViewSet
        from apps.sales.api.offline_views import (
            OfflineAuthorizationViewSet,
            OfflineDeviceViewSet,
        )

        for viewset, action, scope in [
            (PushSubscriptionViewSet, "create", "notifications_write"),
            (OfflineDeviceViewSet, "create", "offline_sync"),
            (OfflineAuthorizationViewSet, "create", "offline_sync"),
        ]:
            v = viewset()
            v.action = action
            v.get_throttles()
            assert v.throttle_scope == scope

    def test_health_probes_are_not_throttled(self):
        from apps.core.api.views import HealthView, LivenessView, ReadinessView

        for view in (HealthView, LivenessView, ReadinessView):
            assert view.throttle_classes == []

    def test_production_throttling_uses_redis_not_locmem(self, monkeypatch):
        import importlib
        import sys

        monkeypatch.setenv("DJANGO_SKIP_PROD_VALIDATION", "1")
        for k, v in {
            "SECRET_KEY": "x" * 60,
            "DATABASE_URL": "postgres://u:p@db/app",
            "REDIS_URL": "redis://cache:6379/0",
            "ALLOWED_HOSTS": "api.example.com",
            "CORS_ALLOWED_ORIGINS": "https://app.example.com",
            "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
            "OFFLINE_SIGNING_KEY": "y" * 40,
            "DJANGO_STORAGE_BACKEND": "local",
        }.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("config.settings.production", None)
        prod = importlib.import_module("config.settings.production")
        assert prod.CACHES["default"]["BACKEND"] == "django_redis.cache.RedisCache"
        assert "locmem" not in prod.CACHES["default"]["BACKEND"].lower()


class TestThrottleEnforcement:
    def test_login_scope_returns_safe_envelope_and_429(self, api_client, monkeypatch):
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**SimpleRateThrottle.THROTTLE_RATES, "auth_login": "2/min"},
        )
        codes = []
        for _ in range(4):
            res = api_client.post(
                f"{API}/auth/login/",
                {"username": "nobody", "password": "wrong-pass-123"},
            )
            codes.append(res.status_code)
        assert 429 in codes
        throttled = next(
            api_client.post(
                f"{API}/auth/login/",
                {"username": "nobody", "password": "wrong-pass-123"},
            )
            for _ in [1]
        )
        assert throttled.status_code == 429
        body = throttled.json()
        assert set(body) == {"code", "message", "field_errors", "request_id"}
        assert body["code"] == "throttled"
        assert throttled.headers.get("Retry-After")

    def test_429_logs_a_scrubbed_security_event(self, api_client, monkeypatch, caplog):
        from rest_framework.throttling import SimpleRateThrottle

        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**SimpleRateThrottle.THROTTLE_RATES, "auth_login": "1/min"},
        )
        secret_pw = "sup3r-secret-passphrase"
        with caplog.at_level(logging.WARNING, logger="apps.security"):
            for _ in range(3):
                api_client.post(
                    f"{API}/auth/login/",
                    {"username": "bob", "password": secret_pw},
                )

        events = [r for r in caplog.records if r.name == "apps.security"]
        assert events, "no security_event was logged for the 429"
        rec = events[-1]
        assert rec.getMessage() == "security_event"
        assert getattr(rec, "event", None) in {"rate_limited", "axes_lockout"}
        assert getattr(rec, "code", None) in {"throttled", "too_many_attempts"}
        assert getattr(rec, "path", "").endswith("/auth/login/")
        # nothing from the request body reaches the log record
        flat = " ".join(f"{k}={v}" for k, v in rec.__dict__.items())
        assert secret_pw not in flat
        assert "password" not in flat.lower()
        assert "wrong" not in flat.lower()
