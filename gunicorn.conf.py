"""Gunicorn configuration for the production image.

Tuning notes
------------
* ``workers``      : (2 x CPU) + 1 by default; override with ``WEB_CONCURRENCY``.
* ``worker_class`` : ``gthread`` — the app is I/O bound (DB, Redis) and thread
                     workers keep memory low on a small Railway instance.
* ``threads``      : 4 per worker (``GUNICORN_THREADS``).
* ``timeout``      : 30s hard limit on a request (``GUNICORN_TIMEOUT``).
* ``graceful_timeout`` : 30s to finish in-flight requests on SIGTERM before the
                     worker is killed — lets Railway roll deploys without 502s.
* ``max_requests`` : recycle a worker after N requests to bound memory growth.
"""

import multiprocessing
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = _int("WEB_CONCURRENCY", (multiprocessing.cpu_count() * 2) + 1)
worker_class = "gthread"
threads = _int("GUNICORN_THREADS", 4)
timeout = _int("GUNICORN_TIMEOUT", 30)
graceful_timeout = _int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int("GUNICORN_KEEPALIVE", 5)
max_requests = _int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _int("GUNICORN_MAX_REQUESTS_JITTER", 100)

# Structured access/error logs to stdout/stderr for Railway to capture.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")
