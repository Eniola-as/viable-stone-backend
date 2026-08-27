"""drf-spectacular extensions."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class CSRFSessionScheme(OpenApiAuthenticationExtension):
    target_class = "apps.core.authentication.CSRFSessionAuthentication"
    name = "cookieAuth"

    def get_security_definition(self, auto_schema):
        from django.conf import settings

        return {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.SESSION_COOKIE_NAME,
        }
