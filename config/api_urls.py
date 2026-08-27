"""All `/api/v1/` routes."""

from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.core.api.views import HealthView

app_name = "api"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="api:schema"),
        name="docs",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="api:schema"),
        name="redoc",
    ),
    path("auth/", include("apps.accounts.api.auth_urls")),
    path("", include("apps.accounts.api.urls")),
    path("", include("apps.core.api.urls")),
    path("", include("apps.catalog.api.urls")),
    path("", include("apps.inventory.api.urls")),
    path("", include("apps.sales.api.urls")),
    path("", include("apps.finance.api.urls")),
]
