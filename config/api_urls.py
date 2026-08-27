"""All `/api/v1/` routes."""

from django.urls import include, path

from apps.core.api.docs import (
    GatedRedocView,
    GatedSchemaView,
    GatedSwaggerView,
)
from apps.core.api.views import HealthView, LivenessView, ReadinessView

app_name = "api"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("health/live/", LivenessView.as_view(), name="health-live"),
    path("health/ready/", ReadinessView.as_view(), name="health-ready"),
    path("schema/", GatedSchemaView.as_view(), name="schema"),
    path("docs/", GatedSwaggerView.as_view(url_name="api:schema"), name="docs"),
    path("redoc/", GatedRedocView.as_view(url_name="api:schema"), name="redoc"),
    path("auth/", include("apps.accounts.api.auth_urls")),
    path("", include("apps.accounts.api.urls")),
    path("", include("apps.core.api.urls")),
    path("", include("apps.catalog.api.urls")),
    path("", include("apps.inventory.api.urls")),
    path("", include("apps.sales.api.urls")),
    path("", include("apps.sales.api.offline_urls")),
    path("", include("apps.finance.api.urls")),
    path("", include("apps.notifications.api.urls")),
]
