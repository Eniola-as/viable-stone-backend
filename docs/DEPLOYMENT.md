# Deployment

Nothing here has been deployed. This is the runbook for when the shop is ready.

## Image

`Dockerfile` builds a production image:

* base `python:3.13.15-slim-bookworm`, pinned by its **verified**
  multi-architecture manifest-list digest
  (`sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca`,
  resolved from Docker Hub `library/python` on 2026-08-27 and confirmed equal
  to `python:3.13-slim-bookworm`). Python **3.13** is used consistently in the
  image, CI (`actions/setup-python` with `python-version: "3.13"`) and
  `pyproject.toml` (`requires-python = ">=3.13,<3.14"`).
* multi-stage; runtime dependencies only, installed from `requirements.lock`
  into `/venv`.
* runs as the non-root `app` user.
* `collectstatic` runs at build time with throwaway `config.settings.build`
  settings; WhiteNoise serves the hashed static assets.
* `HEALTHCHECK` hits `/api/v1/health/live/`.
* entrypoint: `gunicorn config.wsgi:application -c gunicorn.conf.py`
  (`gunicorn.conf.py` documents workers / threads / timeout / graceful
  shutdown; override via `WEB_CONCURRENCY`, `GUNICORN_*`).

## Container vulnerability scanning

CI's `docker` job scans the built image with Trivy (`aquasecurity/trivy-action`,
pinned to a verified 40-character commit SHA). Configuration:
`severity: HIGH,CRITICAL`, `vuln-type: os,library`, `exit-code: "1"`,
**`ignore-unfixed: false`** — the build **fails on every HIGH or CRITICAL OS or
application vulnerability, fixed or unfixed**.

The only way to pass with a known HIGH/CRITICAL finding is to add its exact CVE
id to `.trivyignore` with a full justification block (CVE id, package, reason,
residual-risk assessment, approver + date, review-by date). Broad severity or
"unfixed" suppression is not allowed, and an entry must never be added just to
make CI green — bump the pinned base-image digest or dependency instead. There
are **no approved exclusions** at present.

If the pinned base image ships an unpatched HIGH/CRITICAL, this job will fail;
the fix is to bump `Dockerfile`'s pinned digest (Dependabot raises that PR) or,
only with sign-off, add a documented `.trivyignore` entry.

Re-run locally: `trivy image viable-stone-backend:local`.

Build & smoke-test locally:

```bash
docker build -t viable-stone-backend:local .
docker run --rm --entrypoint id viable-stone-backend:local -u        # != 0
docker run --rm --entrypoint python viable-stone-backend:local -c "import django;print(django.get_version())"
```

## Static assets

Served by WhiteNoise from the image (`CompressedManifestStaticFilesStorage`).
The container filesystem is **ephemeral** — it must never be used for private
media (`expenses/…`, `restocks/…`). Use `DJANGO_STORAGE_BACKEND=s3` in
production (see `docs/PRODUCTION_ENV.md`).

## Migrations — a controlled release step

**Web workers never run migrations.** Run them once per release, before the new
image serves traffic:

```bash
# one-off, on the release image, with production env:
python manage.py migrate --noinput
```

On Railway, use a **pre-deploy command** / release phase (or a one-off job on
the same image) — not `startCommand`, and not every replica. Migrations in this
project are additive and safe to apply while the previous version still runs;
still, apply them as a single gated step.

Rollback: keep the previous image tag; if a migration must be reverted, restore
from backup (`docs/BACKUP_RESTORE.md`) rather than `migrate <app> <prev>` on
live financial data.

## Health endpoints for the platform

* **Liveness** (container "is it up"): `GET /api/v1/health/live/` → `200`.
* **Readiness** (route traffic?): `GET /api/v1/health/ready/` → `200` when
  PostgreSQL **and** Redis are reachable, `503` otherwise.

Bodies are minimal and contain no versions, credentials or addresses.

## First-deploy checklist

1. Provision managed PostgreSQL 18 and Redis; capture `DATABASE_URL` /
   `REDIS_URL`.
2. Choose and provision the private object-storage bucket; capture `AWS_S3_*`.
3. Set every variable in `docs/PRODUCTION_ENV.md` (required + recommended).
4. Deploy the image; run `migrate` as the release step.
5. `GET /api/v1/health/ready/` → `200`.
6. Enable PostgreSQL PITR / volume snapshots and the off-site encrypted
   `pg_dump` job (`docs/BACKUP_RESTORE.md`).
7. Run one restore drill.
8. (Optional) set `SENTRY_DSN`.
