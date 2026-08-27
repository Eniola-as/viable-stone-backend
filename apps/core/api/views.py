from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.serializers import HealthSerializer


class HealthView(APIView):
    """Liveness/readiness probe. Public; never leaks configuration."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    @extend_schema(responses={200: HealthSerializer, 503: HealthSerializer}, auth=[])
    def get(self, request):
        database_ok = _check_database()
        cache_ok = _check_cache()
        healthy = database_ok and cache_ok
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
