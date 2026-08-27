"""Test settings.

PostgreSQL is kept (never SQLite) so transaction and concurrency tests run
against the real database engine.
"""

import tempfile

from .base import *
from .base import env

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# django-axes hits the cache on every auth attempt; disable globally and
# re-enable per-test with override_settings where lockout behaviour is asserted.
AXES_ENABLED = False

# django-otp throttles a device for ~1s after a bad code; that makes a
# wrong-then-right assertion in one test flaky. Rate limiting is covered by the
# DRF ScopedRateThrottle + axes tests instead.
OTP_TOTP_THROTTLE_FACTOR = 0

# Fast, deterministic hashing for the test database only.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHE_BACKEND = "locmem"
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "viable-stone-test",
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Isolated upload area so tests never touch real private media.
PRIVATE_MEDIA_ROOT = tempfile.mkdtemp(prefix="vs-test-media-")
MEDIA_ROOT = PRIVATE_MEDIA_ROOT

CORS_ALLOWED_ORIGINS = ["http://localhost:3000"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:3000"]

# Keep the configured PostgreSQL connection; Django prefixes the name with
# ``test_``. Allow a CI override.
DATABASES["default"]["TEST"] = {
    "NAME": env("TEST_DB_NAME", default=None),
}
