"""Stage 18 — production admin & debug policy."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.urls import Resolver404, get_resolver, resolve

from tests.acceptance.conftest import reloaded_urlconf

pytestmark = pytest.mark.django_db


class TestAdminRouting:
    def test_admin_is_available_in_development(self):
        with reloaded_urlconf(ADMIN_ENABLED=True):
            match = resolve("/admin/login/")
            assert match is not None

    def test_admin_returns_404_when_disabled_for_anonymous(self, api_client):
        with reloaded_urlconf(ADMIN_ENABLED=False):
            for path in ("/admin/", "/admin/login/", "/admin/accounts/user/"):
                with pytest.raises(Resolver404):
                    resolve(path)
                assert api_client.get(path).status_code == 404

    def test_admin_returns_404_when_disabled_for_authenticated_owner(
        self, api_client, login_as, owner
    ):
        with reloaded_urlconf(ADMIN_ENABLED=False):
            client = login_as(owner)
            assert client.get("/admin/").status_code == 404
            assert client.get("/admin/accounts/user/").status_code == 404

    def test_no_alternate_admin_url_exists(self):
        with reloaded_urlconf(ADMIN_ENABLED=False):
            resolver = get_resolver()
            admin_like = [
                str(p.pattern)
                for p in _flatten(resolver)
                if "admin" in str(p.pattern).lower()
                or "django-admin" in str(p.pattern).lower()
            ]
            assert admin_like == [], admin_like


def _flatten(resolver):
    for pattern in resolver.url_patterns:
        if hasattr(pattern, "url_patterns"):
            yield from _flatten(pattern)
        else:
            yield pattern


def _load_production_settings(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("DJANGO_SKIP_PROD_VALIDATION", "1")
    for key, value in {
        "SECRET_KEY": "x" * 60,
        "DATABASE_URL": "postgres://u:p@db/app",
        "REDIS_URL": "redis://cache:6379/0",
        "ALLOWED_HOSTS": "api.example.com",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "OFFLINE_SIGNING_KEY": "y" * 40,
        "DJANGO_STORAGE_BACKEND": "local",
    }.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("config.settings.production", None)
    return importlib.import_module("config.settings.production")


class TestDebugTooling:
    def test_debug_is_false_in_production_settings(self, monkeypatch):
        prod = _load_production_settings(monkeypatch)
        assert prod.DEBUG is False
        assert prod.ADMIN_ENABLED is False
        assert prod.API_DOCS_ENABLED is False

    def test_no_debug_or_profiling_apps_installed(self):
        banned = {
            "debug_toolbar",
            "silk",
            "django_extensions",
            "django_cprofile_middleware",
            "nplusone",
        }
        assert banned.isdisjoint(set(settings.INSTALLED_APPS))

    def test_no_debug_toolbar_middleware(self):
        joined = " ".join(settings.MIDDLEWARE).lower()
        assert "debug_toolbar" not in joined
        assert "silk" not in joined

    def test_no_debug_routes_are_registered(self):
        resolver = get_resolver()
        bad = [
            str(p.pattern)
            for p in _flatten(resolver)
            if any(
                token in str(p.pattern).lower()
                for token in ("__debug__", "debug_toolbar", "silk/", "django-silk")
            )
        ]
        assert bad == [], bad


class TestApiDocsPolicyUnchanged:
    """Owner/tech-admin + gate policy from Stage 17 must still hold."""

    def test_schema_requires_owner_or_techadmin_in_prod_mode(
        self, api_client, login_as, employee, owner
    ):
        from django.test import override_settings

        with override_settings(API_DOCS_ENABLED=True, DEBUG=False):
            assert api_client.get("/api/v1/schema/").status_code in (401, 403)
            assert login_as(employee).get("/api/v1/schema/").status_code == 403
            assert login_as(owner).get("/api/v1/schema/").status_code == 200

    def test_schema_404_when_disabled(self, login_as, owner):
        from django.test import override_settings

        with override_settings(API_DOCS_ENABLED=False, DEBUG=False):
            assert login_as(owner).get("/api/v1/schema/").status_code == 404


class TestHealthLeakage:
    def test_health_bodies_are_minimal(self, api_client):
        forbidden = (
            "redis://",
            "postgres://",
            "postgresql://",
            "secret",
            "password",
            "traceback",
            "/users/",
            "c:\\",
            "settings",
            "django",
        )
        for path in (
            "/api/v1/health/",
            "/api/v1/health/live/",
            "/api/v1/health/ready/",
        ):
            body = api_client.get(path).content.decode().lower()
            assert not any(token in body for token in forbidden), (path, body)
            assert "version" not in body
