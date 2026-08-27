"""Stage 17 — separate liveness / readiness probes; minimal, leak-free bodies."""

import pytest

pytestmark = pytest.mark.django_db

_INFRA_HINTS = ("redis://", "postgres://", "postgresql://", "amazonaws", "railway")


class TestLiveness:
    def test_liveness_is_ok_without_touching_dependencies(self, api_client):
        res = api_client.get("/api/v1/health/live/")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

    def test_liveness_needs_no_auth(self, api_client):
        assert api_client.get("/api/v1/health/live/").status_code == 200


class TestReadiness:
    def test_readiness_reports_ready_when_dependencies_are_up(self, api_client):
        res = api_client.get("/api/v1/health/ready/")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ready"
        assert body["database"] == "ok"

    def test_readiness_is_503_when_required_redis_is_down(self, api_client, settings):
        settings.REDIS_HEALTHCHECK = True
        settings.CACHES = {
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": "redis://127.0.0.1:6399/0",  # nothing listening
                "OPTIONS": {
                    "CLIENT_CLASS": "django_redis.client.DefaultClient",
                    "IGNORE_EXCEPTIONS": False,
                },
            }
        }
        res = api_client.get("/api/v1/health/ready/")
        assert res.status_code == 503
        assert res.json()["status"] == "unavailable"
        assert res.json()["redis"] == "error"

    def test_health_bodies_reveal_no_infrastructure_details(self, api_client):
        for path in (
            "/api/v1/health/",
            "/api/v1/health/live/",
            "/api/v1/health/ready/",
        ):
            text = api_client.get(path).content.decode().lower()
            assert not any(hint in text for hint in _INFRA_HINTS)
            assert "version" not in text
