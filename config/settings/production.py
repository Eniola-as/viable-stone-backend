"""Production settings.

Nothing here is deployed by this repository. It exists so the backend can be
built, checked and audited (``manage.py check --deploy``, CI) against realistic
production configuration. Required environment variables are validated at import
time; a missing one stops startup with one clear error.
"""

import os

from .base import *
from .base import BASE_DIR, LOGGING, env
from .validation import validate_production_config

# --------------------------------------------------------------------------- #
# Core                                                                        #
# --------------------------------------------------------------------------- #
DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")

# Never a wildcard credentialed CORS policy.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True

# --------------------------------------------------------------------------- #
# Transport / cookie / proxy security                                         #
# --------------------------------------------------------------------------- #
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True
)
# Do NOT enable preload until a real, stable apex domain is confirmed
# (blueprint SECURITY HARDENING 3). W021 is deliberately silenced until then.
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SILENCED_SYSTEM_CHECKS = ["security.W021"]

# --------------------------------------------------------------------------- #
# Cache / Redis — required, no silent fallback                                #
# --------------------------------------------------------------------------- #
REDIS_URL = env("REDIS_URL")  # required; ImproperlyConfigured if absent
REDIS_HEALTHCHECK = True
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": False,
        },
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
# DRF throttling and django-axes counters share this Redis cache (see base.py).

# --------------------------------------------------------------------------- #
# API documentation — locked down                                             #
# --------------------------------------------------------------------------- #
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)

# --------------------------------------------------------------------------- #
# Logging — structured JSON to stdout                                         #
# --------------------------------------------------------------------------- #
LOGGING["handlers"]["console"]["formatter"] = "json"

# --------------------------------------------------------------------------- #
# Private file storage                                                        #
# --------------------------------------------------------------------------- #
# 'local' keeps private media on disk (fine for a first build, NOT durable in a
# container). 's3' targets a generic private S3-compatible bucket entirely from
# environment variables — this repo never creates or chooses a provider.
DJANGO_STORAGE_BACKEND = env("DJANGO_STORAGE_BACKEND", default="local")

STORAGES = {
    "default": {"BACKEND": "apps.core.storage.PrivateMediaStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

if DJANGO_STORAGE_BACKEND == "s3":
    # Requires: pip install "django-storages[s3]"  (declared in the
    # `production` optional-dependencies extra).
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
    AWS_S3_ACCESS_KEY_ID = env("AWS_S3_ACCESS_KEY_ID")
    AWS_S3_SECRET_ACCESS_KEY = env("AWS_S3_SECRET_ACCESS_KEY")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="")
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_DEFAULT_ACL = None  # private objects only — never public-read
    AWS_QUERYSTRING_AUTH = True  # short-lived signed download URLs
    AWS_QUERYSTRING_EXPIRE = env.int("AWS_QUERYSTRING_EXPIRE", default=300)
    AWS_S3_FILE_OVERWRITE = False
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"default_acl": None, "querystring_auth": True},
    }

STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------- #
# Error monitoring — inert without SENTRY_DSN                                  #
# --------------------------------------------------------------------------- #
from apps.core.sentry import configure_sentry

SENTRY_ENABLED = configure_sentry(env)

# --------------------------------------------------------------------------- #
# Fail fast on invalid production configuration                               #
# --------------------------------------------------------------------------- #
if os.environ.get("DJANGO_SKIP_PROD_VALIDATION") != "1":
    validate_production_config(env, debug=DEBUG)
