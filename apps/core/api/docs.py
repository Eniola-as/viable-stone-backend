"""API documentation views, gated for production.

The OpenAPI schema and the Swagger / ReDoc UIs are open in development. In
production they are either disabled entirely (``API_DOCS_ENABLED=False``, the
default) or restricted to the owner and the technical administrator.
"""

from __future__ import annotations

from django.conf import settings
from django.http import Http404
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.core.permissions import IsOwnerOrTechAdmin


class _GatedDocsMixin:
    def initial(self, request, *args, **kwargs):
        if not getattr(settings, "API_DOCS_ENABLED", settings.DEBUG):
            raise Http404()
        return super().initial(request, *args, **kwargs)

    def get_permissions(self):
        if getattr(settings, "DEBUG", False):
            return []
        return [IsOwnerOrTechAdmin()]


class GatedSchemaView(_GatedDocsMixin, SpectacularAPIView):
    pass


class GatedSwaggerView(_GatedDocsMixin, SpectacularSwaggerView):
    pass


class GatedRedocView(_GatedDocsMixin, SpectacularRedocView):
    pass
