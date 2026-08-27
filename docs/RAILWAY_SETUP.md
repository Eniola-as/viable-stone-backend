# Railway Setup (for later — nothing deployed yet)

Documented steps only. **No `railway.json` / `railway.toml` config-as-code**
files are added — Railway's schema shifts and a stale committed file is worse
than none. Configure the project in the Railway dashboard when the shop is
ready.

## Services to create

1. **PostgreSQL** — managed Postgres **18**. Enable scheduled volume backups;
   on the Pro tier enable PITR. Note the connection string → `DATABASE_URL`.
2. **Redis** — managed Redis. Note the connection string → `REDIS_URL`.
   Redis is cache / throttle / offline-session coordination only; losing it is
   harmless (see `docs/BACKUP_RESTORE.md`).
3. **Backend (this repo)** — Docker deploy from `Dockerfile`.
   * Deploy trigger: pushes to `main` (or manual).
   * **Pre-deploy / release command:** `python manage.py migrate --noinput`
     (runs once per release, not per replica — see `docs/DEPLOYMENT.md`).
   * **Start command:** leave empty — the image `CMD` runs Gunicorn.
   * **Health check path:** `/api/v1/health/ready/` (readiness). Also expose
     `/api/v1/health/live/` if the platform wants a separate liveness path.
4. **Object storage** — a **private** S3-compatible bucket for expense /
   restock documents. Railway Buckets currently lack server-side encryption,
   versioning and object-lock; prefer Cloudflare R2 / Backblaze B2 with
   versioning + lifecycle. Set `DJANGO_STORAGE_BACKEND=s3` + `AWS_S3_*`.

## Environment variables

Set everything in `docs/PRODUCTION_ENV.md` on the Backend service. Railway
injects `DATABASE_URL` / `REDIS_URL` automatically when the plugins are linked —
verify the names match.

## Networking / security

* Custom domain on the Backend service → put its host in `ALLOWED_HOSTS` and
  the SPA origin in `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` (exact,
  no wildcard).
* Railway terminates TLS and sets `X-Forwarded-Proto`; the app already trusts
  that header (`SECURE_PROXY_SSL_HEADER`) and force-redirects to HTTPS.
* Keep `API_DOCS_ENABLED=false` (or owner/tech-admin only).
* `SECURE_HSTS_PRELOAD=false` until the apex domain is final and stable.

## Deploy sequence (first time)

1. Create Postgres + Redis + object storage; copy their credentials.
2. Create the Backend service from this repo's `Dockerfile`.
3. Add all env vars (`docs/PRODUCTION_ENV.md`).
4. Set the pre-deploy `migrate` command and the `/api/v1/health/ready/` health
   check.
5. Deploy. Confirm `GET /api/v1/health/ready/` → `200`.
6. Enable backups + PITR; run one restore drill.
7. `POST /api/v1/auth/login/` as the seeded owner, complete MFA, change the
   password.

## CI relationship

`.github/workflows/ci.yml` runs lint, the full PostgreSQL + real-Redis test
suite, coverage ≥ 90 %, OpenAPI validation, `check --deploy`, `pip-audit`, a
secret-leak scan and a Docker build + non-root smoke test. **It never deploys
and never uses production credentials.** Deployment is a manual, dashboard-first
action.
