"""
Base Django settings for the Viable Stone paint-shop backend.

Environment-specific overrides live in ``development.py``, ``test.py`` and
``production.py``. Secrets are read from ``.env`` via ``django-environ`` and are
never committed.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# --------------------------------------------------------------------------- #
# Core                                                                        #
# --------------------------------------------------------------------------- #

SECRET_KEY = env("SECRET_KEY")

# Overridden per environment; base default is the safe (production) value.
DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# --------------------------------------------------------------------------- #
# Applications                                                                #
# --------------------------------------------------------------------------- #

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.catalog",
    "apps.inventory",
    "apps.sales",
    "apps.finance",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.ActivityTrackingMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# Database                                                                    #
# --------------------------------------------------------------------------- #

DATABASES = {
    "default": {
        **env.db("DATABASE_URL"),
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
    }
}

# --------------------------------------------------------------------------- #
# Cache / Redis                                                               #
# --------------------------------------------------------------------------- #
# Redis backs shared caching, DRF throttling and django-axes counters. Local
# development and tests may fall back to in-process memory so the stack runs
# without a Redis server (docker compose up brings real Redis on 127.0.0.1).
# Production *requires* REDIS_URL and never falls back — see production.py.

REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CACHE_BACKEND = env("CACHE_BACKEND", default="locmem")
# Set True where Redis is a hard dependency; the readiness probe then pings it.
REDIS_HEALTHCHECK = env.bool("REDIS_HEALTHCHECK", default=False)

if CACHE_BACKEND == "redis":
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
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "viable-stone",
        }
    }

# API docs / schema: open in dev, gated to owner+tech-admin or disabled in prod.
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)

# The Django admin is a development convenience. It is routed only when this is
# true; production.py sets it False so /admin/ 404s. There is no alternate URL.
ADMIN_ENABLED = env.bool("DJANGO_ADMIN_ENABLED", default=True)

# --------------------------------------------------------------------------- #
# Authentication                                                              #
# --------------------------------------------------------------------------- #

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # AxesStandaloneBackend must be first so lockouts are enforced.
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

_PW = "django.contrib.auth.password_validation"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"{_PW}.UserAttributeSimilarityValidator"},
    {"NAME": f"{_PW}.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": f"{_PW}.CommonPasswordValidator"},
    {"NAME": f"{_PW}.NumericPasswordValidator"},
]

LOGIN_URL = "/api/v1/auth/login/"

# --------------------------------------------------------------------------- #
# django-axes (brute-force protection)                                        #
# --------------------------------------------------------------------------- #

AXES_ENABLED = env.bool("AXES_ENABLED", default=True)
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = env.float("AXES_COOLOFF_HOURS", default=0.25)  # 15 minutes
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_PARAMETERS = ["ip_address", "username"]
AXES_CACHE = "default"
AXES_DISABLE_ACCESS_LOG = False
AXES_LOCKOUT_CALLABLE = "apps.accounts.auth.lockout_response"

# --------------------------------------------------------------------------- #
# Internationalisation                                                        #
# --------------------------------------------------------------------------- #

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- #
# Static & media                                                              #
# --------------------------------------------------------------------------- #

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files (product images, expense receipts, supplier invoices) are
# private: stored outside the static tree and served only through an
# authenticated, branch-scoped download view.
PRIVATE_MEDIA_ROOT = env.path("PRIVATE_MEDIA_ROOT", default=BASE_DIR / "private-media")
MEDIA_URL = "/api/v1/files/"
MEDIA_ROOT = PRIVATE_MEDIA_ROOT

STORAGES = {
    "default": {"BACKEND": "apps.core.storage.PrivateMediaStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Upload hard limits (bytes). Individual serializers narrow this further.
MAX_UPLOAD_SIZE = env.int("MAX_UPLOAD_SIZE", default=5 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int(
    "DATA_UPLOAD_MAX_MEMORY_SIZE", default=6 * 1024 * 1024
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
ALLOWED_UPLOAD_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf"]
ALLOWED_UPLOAD_MIME_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- #
# CORS / CSRF                                                                 #
# --------------------------------------------------------------------------- #

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "vs_sessionid"
CSRF_COOKIE_NAME = "vs_csrftoken"
# The SPA must read the CSRF token from the cookie, so it is not HttpOnly.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_USE_SESSIONS = False
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 12)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# --------------------------------------------------------------------------- #
# Django REST Framework                                                       #
# --------------------------------------------------------------------------- #

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.CSRFSessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.core.permissions.IsAuthenticatedAndMFAVerified",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    # A global baseline for every endpoint (anon by IP, authenticated by user),
    # plus per-view ScopedRateThrottle for high-risk operations. Health probes
    # set throttle_classes = [] so infrastructure can poll them freely.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", default="120/min"),
        "user": env("THROTTLE_USER", default="2000/min"),
        "auth_login": env("THROTTLE_AUTH_LOGIN", default="10/min"),
        "auth_mfa": env("THROTTLE_AUTH_MFA", default="10/min"),
        "auth_recovery": env("THROTTLE_AUTH_RECOVERY", default="5/min"),
        "sales_write": env("THROTTLE_SALES_WRITE", default="120/min"),
        "offline_sync": env("THROTTLE_OFFLINE_SYNC", default="60/min"),
        "notifications_write": env("THROTTLE_NOTIFICATIONS_WRITE", default="60/min"),
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "COERCE_DECIMAL_TO_STRING": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Viable Stone Paint Shop API",
    "DESCRIPTION": (
        "Internal management API for the Viable Stone paint and painting-"
        "equipment shop. Source of truth for authentication, prices, stock, "
        "sales, payments, receipts, expenses, approvals and reports."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": True,
    "ENUM_NAME_OVERRIDES": {},
    "SERVERS": [{"url": "/", "description": "Current host"}],
}

# --------------------------------------------------------------------------- #
# Security defaults (production tightens these further)                        #
# --------------------------------------------------------------------------- #

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --------------------------------------------------------------------------- #
# Web Push (VAPID)                                                            #
# --------------------------------------------------------------------------- #
# Browser push is best-effort only; the durable in-app Notification is always
# the source of truth. The PRIVATE key is a secret and lives only in the
# environment — it is never exposed by an API, a log line or openapi.yml. Only
# the browser-safe PUBLIC key is served (to the SPA's service worker).

VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_ADMIN_EMAIL = env("VAPID_ADMIN_EMAIL", default="")
PUSH_DEFAULT_TTL = env.int("PUSH_DEFAULT_TTL", default=600)
# Nothing is sent over the network unless a full key pair is configured.
PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_ADMIN_EMAIL)

# --------------------------------------------------------------------------- #
# Sentry (enabled only when SENTRY_DSN is provided)                           #
# --------------------------------------------------------------------------- #

SENTRY_DSN = env("SENTRY_DSN", default="")

# --------------------------------------------------------------------------- #
# Logging — request-id aware, structured stdout, never logs secrets           #
# --------------------------------------------------------------------------- #
# LOG_FORMAT=json emits one JSON object per line (production / Railway);
# anything else keeps the human-readable console format for local development.

LOG_FORMAT = env("LOG_FORMAT", default="console")
_LOG_FORMATTER = "json" if LOG_FORMAT == "json" else "standard"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.core.logging.RequestIDFilter"},
    },
    "formatters": {
        "standard": {
            "format": (
                "%(asctime)s %(levelname)s %(name)s [req:%(request_id)s] %(message)s"
            ),
        },
        "json": {"()": "apps.core.logging.JSONFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": _LOG_FORMATTER,
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": env("APP_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}

# --------------------------------------------------------------------------- #
# Business constants                                                          #
# --------------------------------------------------------------------------- #

CURRENCY_CODE = "NGN"
MONEY_DECIMAL_PLACES = 2
OFFLINE_AUTHORIZATION_MAX_HOURS = 24

# Offline fixed-price checkout. The signing key is a server-side secret used to
# sign the catalogue authorization package; it is read only from the
# environment and falls back to SECRET_KEY (also env-only) when unset.
OFFLINE_SIGNING_KEY = env("OFFLINE_SIGNING_KEY", default=SECRET_KEY)
OFFLINE_SYNC_MAX_BATCH = env.int("OFFLINE_SYNC_MAX_BATCH", default=200)

# Single source of truth for the shop's identity on printed documents
# (receipts). Never hard-code any of this inside a template.
BUSINESS_IDENTITY = {
    "name": env("BUSINESS_NAME", default="Viable Stone Paints & Coatings Enterprise"),
    "phone": env("BUSINESS_PHONE", default=""),
    "email": env("BUSINESS_EMAIL", default=""),
    "address": env("BUSINESS_ADDRESS", default=""),
    # Absolute path, or relative to BASE_DIR, to a logo image (PNG/JPG).
    "logo_path": env("BUSINESS_LOGO_PATH", default=""),
    "currency_symbol": env("BUSINESS_CURRENCY_SYMBOL", default="NGN"),
}
