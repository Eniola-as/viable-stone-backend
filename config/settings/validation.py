"""Fail-fast validation of required production configuration.

Called at the end of ``production.py`` so a misconfigured deployment stops at
import time with one clear, aggregated error instead of a confusing runtime
failure later.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

# Keys that must be present and non-empty in the environment for production.
REQUIRED_PRODUCTION_ENV = (
    "SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "ALLOWED_HOSTS",
    "CORS_ALLOWED_ORIGINS",
    "CSRF_TRUSTED_ORIGINS",
    "OFFLINE_SIGNING_KEY",
    "DJANGO_STORAGE_BACKEND",
)


def missing_production_env(env, keys=REQUIRED_PRODUCTION_ENV) -> list[str]:
    missing = []
    for key in keys:
        try:
            value = env(key)
        except ImproperlyConfigured:
            missing.append(key)
            continue
        if value is None or str(value).strip() == "":
            missing.append(key)
    return missing


def validate_production_config(env, *, debug: bool, keys=REQUIRED_PRODUCTION_ENV):
    problems: list[str] = []

    missing = missing_production_env(env, keys)
    if missing:
        problems.append(
            "Missing/empty required environment variables: " + ", ".join(missing)
        )

    if debug:
        problems.append("DEBUG must be False in production.")

    backend = ""
    try:
        backend = env("DJANGO_STORAGE_BACKEND", default="local")
    except ImproperlyConfigured:
        backend = "local"
    if backend not in {"local", "s3"}:
        problems.append(
            f"DJANGO_STORAGE_BACKEND must be 'local' or 's3', got {backend!r}."
        )
    if backend == "s3":
        s3_missing = [
            k
            for k in (
                "AWS_STORAGE_BUCKET_NAME",
                "AWS_S3_ENDPOINT_URL",
                "AWS_S3_ACCESS_KEY_ID",
                "AWS_S3_SECRET_ACCESS_KEY",
            )
            if not env(k, default="")
        ]
        if s3_missing:
            problems.append("S3 storage selected but missing: " + ", ".join(s3_missing))

    if problems:
        raise ImproperlyConfigured(
            "Production configuration is invalid:\n  - " + "\n  - ".join(problems)
        )
