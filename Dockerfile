# syntax=docker/dockerfile:1
# Production image. Nothing here is deployed by this repository; it exists so
# the image can be built and smoke-tested locally.

# --------------------------------------------------------------------------- #
# Builder — install locked runtime dependencies into an isolated virtualenv    #
# --------------------------------------------------------------------------- #
FROM python:3.13.1-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-virtualenv -r requirements.lock

# --------------------------------------------------------------------------- #
# Runtime — slim, non-root, runtime dependencies only                         #
# --------------------------------------------------------------------------- #
FROM python:3.13.1-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    LOG_FORMAT=json

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /venv /venv
COPY --chown=app:app . .

# Collect static assets with throwaway build settings (no real secrets needed).
RUN DJANGO_SETTINGS_MODULE=config.settings.build \
    python manage.py collectstatic --noinput --clear

USER app
EXPOSE 8000

# Liveness only — the container is "up" once the process answers; readiness
# (DB + Redis) is polled separately by the platform.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live/', timeout=4).status == 200 else 1)"

# Database migrations are a controlled release step (see docs/DEPLOYMENT.md);
# web workers never run them. Start the app server only:
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
