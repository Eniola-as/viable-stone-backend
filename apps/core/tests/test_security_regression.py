"""Stage 17 — explicit security-configuration regression tests."""

import importlib

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from config.settings.validation import (
    REQUIRED_PRODUCTION_ENV,
    validate_production_config,
)

pytestmark = pytest.mark.django_db


class _FakeEnv:
    """Minimal stand-in for django-environ's ``env`` callable."""

    def __init__(self, values):
        self._values = values

    def __call__(self, key, default=None):
        if key in self._values:
            return self._values[key]
        if default is None and key not in self._values:
            raise ImproperlyConfigured(key)
        return default

    def bool(self, key, default=False):
        return bool(self._values.get(key, default))

    def int(self, key, default=0):
        return int(self._values.get(key, default))

    def float(self, key, default=0.0):
        return float(self._values.get(key, default))

    def list(self, key, default=None):
        return self._values.get(key, default or [])


_FULL_ENV = {
    "SECRET_KEY": "x" * 60,
    "DATABASE_URL": "postgres://u:p@db/app",
    "REDIS_URL": "redis://cache:6379/0",
    "ALLOWED_HOSTS": "api.example.com",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
    "OFFLINE_SIGNING_KEY": "y" * 40,
    "DJANGO_STORAGE_BACKEND": "local",
}


class TestProductionEnvValidation:
    def test_passes_with_a_complete_environment(self):
        validate_production_config(_FakeEnv(dict(_FULL_ENV)), debug=False)

    @pytest.mark.parametrize("missing", sorted(REQUIRED_PRODUCTION_ENV))
    def test_fails_when_a_required_var_is_missing(self, missing):
        values = {k: v for k, v in _FULL_ENV.items() if k != missing}
        with pytest.raises(ImproperlyConfigured) as err:
            validate_production_config(_FakeEnv(values), debug=False)
        assert missing in str(err.value)

    def test_fails_when_debug_is_true(self):
        with pytest.raises(ImproperlyConfigured) as err:
            validate_production_config(_FakeEnv(dict(_FULL_ENV)), debug=True)
        assert "DEBUG must be False" in str(err.value)

    def test_s3_backend_requires_bucket_credentials(self):
        values = {**_FULL_ENV, "DJANGO_STORAGE_BACKEND": "s3"}
        with pytest.raises(ImproperlyConfigured) as err:
            validate_production_config(_FakeEnv(values), debug=False)
        assert "S3 storage selected but missing" in str(err.value)


class TestProductionSettingsModule:
    def test_production_settings_are_hard(self, monkeypatch):
        import sys

        for key, value in _FULL_ENV.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("config.settings.production", None)
        prod = importlib.import_module("config.settings.production")

        assert prod.DEBUG is False
        assert prod.SESSION_COOKIE_SECURE is True
        assert prod.CSRF_COOKIE_SECURE is True
        assert prod.SECURE_SSL_REDIRECT is True
        assert prod.SECURE_HSTS_PRELOAD is False  # not until a real domain
        assert prod.SECURE_HSTS_SECONDS >= 31536000
        assert prod.CORS_ALLOW_ALL_ORIGINS is False
        assert prod.API_DOCS_ENABLED is False
        assert prod.REDIS_HEALTHCHECK is True
        assert prod.CACHES["default"]["BACKEND"] == ("django_redis.cache.RedisCache")
        assert prod.LOGGING["handlers"]["console"]["formatter"] == "json"


class TestCorsIsExact:
    def test_no_wildcard_credentialed_cors(self):
        from django.conf import settings

        assert getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False) is False
        for origin in settings.CORS_ALLOWED_ORIGINS:
            assert "*" not in origin
            assert origin.startswith(("http://", "https://"))


class TestApiDocsArePrivate:
    @override_settings(API_DOCS_ENABLED=True, DEBUG=False)
    def test_schema_requires_owner_or_tech_admin_in_production_mode(
        self, api_client, login_as, employee, owner
    ):
        assert api_client.get("/api/v1/schema/").status_code in (401, 403)
        assert login_as(employee).get("/api/v1/schema/").status_code == 403
        assert login_as(owner).get("/api/v1/schema/").status_code == 200

    @override_settings(API_DOCS_ENABLED=False, DEBUG=False)
    def test_schema_is_404_when_disabled(self, login_as, owner):
        assert login_as(owner).get("/api/v1/schema/").status_code == 404

    @override_settings(API_DOCS_ENABLED=True, DEBUG=True)
    def test_schema_is_open_in_development(self, api_client):
        assert api_client.get("/api/v1/schema/").status_code == 200


class TestErrorEnvelopeLeakage:
    def test_500_response_carries_no_traceback_or_internals(
        self, api_client, monkeypatch
    ):
        from apps.core.api import views as core_views

        def _boom():
            raise RuntimeError("DATABASE_URL=postgres://secret would be bad to leak")

        monkeypatch.setattr(core_views, "_check_database", _boom)
        res = api_client.get("/api/v1/health/ready/")
        assert res.status_code == 500
        body = res.json()
        assert set(body) == {"code", "message", "field_errors", "request_id"}
        assert "postgres://" not in res.content.decode()
        assert "Traceback" not in res.content.decode()
