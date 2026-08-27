"""Root URL configuration. All application endpoints live under ``/api/v1/``.

The Django admin is routed only when ``settings.ADMIN_ENABLED`` is true
(development). In production it is not routed at all, so ``/admin/`` — and every
path under it — returns 404 for everyone. There is no alternate admin URL.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("config.api_urls")),
]

if getattr(settings, "ADMIN_ENABLED", settings.DEBUG):
    urlpatterns.insert(0, path("admin/", admin.site.urls))
