from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.serializers import (
    HealthSerializer,
    LivenessSerializer,
    ReadinessSerializer,
)


class _PublicProbe(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []


class LivenessView(_PublicProbe):
    """Process is running. No dependency checks; used by the container probe."""

    @extend_schema(responses={200: LivenessSerializer}, auth=[])
    def get(self, request):
        return Response({"status": "ok"})


class ReadinessView(_PublicProbe):
    """Required dependencies (database + production Redis) are reachable.

    Returns 503 when a required dependency is down. The body is intentionally
    minimal — no versions, credentials or infrastructure addresses.
    """

    @extend_schema(
        responses={200: ReadinessSerializer, 503: ReadinessSerializer}, auth=[]
    )
    def get(self, request):
        checks = {
            "database": _check_database(),
            "cache": _check_cache(),
            "redis": _check_redis(),
        }
        ready = all(checks.values())
        body = {
            "status": "ready" if ready else "unavailable",
            **{name: ("ok" if ok else "error") for name, ok in checks.items()},
        }
        code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(body, status=code)


class HealthView(_PublicProbe):
    """Combined legacy probe (kept for backwards compatibility)."""

    @extend_schema(responses={200: HealthSerializer, 503: HealthSerializer}, auth=[])
    def get(self, request):
        database_ok = _check_database()
        cache_ok = _check_cache()
        redis_ok = _check_redis()
        healthy = database_ok and cache_ok and redis_ok
        body = {
            "status": "ok" if healthy else "degraded",
            "database": "ok" if database_ok else "error",
            "cache": "ok" if cache_ok else "error",
            "time": timezone.now(),
        }
        code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(body, status=code)


def _check_database() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except Exception:  # pragma: no cover - defensive
        return False


def _check_cache() -> bool:
    try:
        cache.set("healthcheck", "1", timeout=5)
        return cache.get("healthcheck") == "1"
    except Exception:  # pragma: no cover - defensive
        return False


def _check_redis() -> bool:
    """Ping Redis directly when it is a required production dependency."""

    if not getattr(settings, "REDIS_HEALTHCHECK", False):
        return True
    try:
        from django_redis import get_redis_connection

        return bool(get_redis_connection("default").ping())
    except Exception:
        return False
