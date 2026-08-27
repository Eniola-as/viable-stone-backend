"""Settings for build-time image tasks only (``collectstatic``).

Never used to serve traffic. It provides throwaway values so ``manage.py``
commands that only touch static files can run without real secrets.
"""

import os

os.environ.setdefault("SECRET_KEY", "build-time-only-not-a-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///build.sqlite3")
os.environ.setdefault("ALLOWED_HOSTS", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from .base import *

DEBUG = False
