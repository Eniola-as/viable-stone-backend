"""Stage 17 — real-Redis integration.

These run against an actual Redis (``docker compose up``, or the CI service
container). They are skipped — never faked — when Redis is unreachable, so the
default local suite still passes without Docker.
"""

import os

import pytest
from django.core.cache import cache

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


def _redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


_REDIS_CACHE = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": False,
        },
    }
}

pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(not _redis_available(), reason="Redis not reachable"),
]


@pytest.fixture(autouse=True)
def _real_redis_cache(settings):
    settings.CACHES = _REDIS_CACHE
    cache.clear()
    yield
    cache.clear()


class TestSharedCache:
    def test_set_get_delete_round_trip(self):
        cache.set("vs:probe", {"n": 1}, timeout=30)
        assert cache.get("vs:probe") == {"n": 1}
        cache.delete("vs:probe")
        assert cache.get("vs:probe") is None

    def test_atomic_incr_is_shared_state(self):
        cache.set("vs:counter", 0)
        for _ in range(5):
            cache.incr("vs:counter")
        assert cache.get("vs:counter") == 5

    def test_value_is_visible_to_an_independent_redis_client(self):
        import redis

        cache.set("vs:shared", "here", timeout=30)
        raw = redis.Redis.from_url(REDIS_URL)
        assert raw.keys("*vs:shared*")


@pytest.mark.django_db
class TestThrottleUsesRedis:
    def test_scoped_throttle_counter_lands_in_redis(self, login_as, owner):
        import redis
        from rest_framework.throttling import SimpleRateThrottle

        SimpleRateThrottle.THROTTLE_RATES = {
            **SimpleRateThrottle.THROTTLE_RATES,
            "notifications_write": "1000/min",
        }
        login_as(owner).post(
            "/api/v1/push-subscriptions/",
            {
                "endpoint": "https://push.example.com/redis-probe",
                "p256dh": "B" * 87,
                "auth": "A" * 22,
            },
            format="json",
        )
        raw = redis.Redis.from_url(REDIS_URL)
        assert raw.keys("*throttle_notifications_write*")


@pytest.mark.django_db
class TestReadinessWithRealRedis:
    def test_ready_when_redis_is_up(self, client, settings):
        settings.REDIS_HEALTHCHECK = True
        res = client.get("/api/v1/health/ready/")
        assert res.status_code == 200
        assert res.json()["redis"] == "ok"
