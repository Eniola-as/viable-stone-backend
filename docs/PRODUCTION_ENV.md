# Production Environment Variables

**Placeholders only — no real secrets in this file or the repo.** Set these in
the Railway service (or any host) environment. `config.settings.production`
validates the required ones at startup and refuses to boot if any are missing.

## Required (startup fails without these)

| Variable | Example placeholder | Notes |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | |
| `SECRET_KEY` | `<50+ random chars>` | `python -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `DATABASE_URL` | `postgres://user:pass@host:5432/viable_stone` | managed PostgreSQL 18 |
| `REDIS_URL` | `redis://default:pass@host:6379/0` | **required**; no fallback |
| `ALLOWED_HOSTS` | `api.viable-stone.example` | comma-separated, exact hosts |
| `CORS_ALLOWED_ORIGINS` | `https://app.viable-stone.example` | exact origins, **no wildcard** |
| `CSRF_TRUSTED_ORIGINS` | `https://app.viable-stone.example` | exact origins |
| `OFFLINE_SIGNING_KEY` | `<32+ random chars>` | signs the offline catalogue package |
| `DJANGO_STORAGE_BACKEND` | `local` or `s3` | see "Private storage" below |

## Recommended

| Variable | Placeholder | Default | Notes |
|---|---|---|---|
| `LOG_FORMAT` | `json` | `console` | structured stdout for Railway |
| `REDIS_HEALTHCHECK` | `true` | `true` in prod | readiness pings Redis |
| `API_DOCS_ENABLED` | `false` | `false` in prod | `true` = owner/tech-admin only |
| `SECURE_SSL_REDIRECT` | `true` | `true` | |
| `SECURE_HSTS_SECONDS` | `31536000` | `31536000` | |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `true` | `true` | |
| `SECURE_HSTS_PRELOAD` | `false` | `false` | **keep false** until a stable apex domain is confirmed |
| `SESSION_COOKIE_AGE` | `43200` | `43200` | seconds |
| `AXES_FAILURE_LIMIT` | `5` | `5` | login lockout threshold |
| `WEB_CONCURRENCY` | `3` | `(2*CPU)+1` | gunicorn workers |
| `GUNICORN_THREADS` | `4` | `4` | |
| `GUNICORN_TIMEOUT` | `30` | `30` | seconds |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | `30` | seconds |

## Monitoring (optional — inert without a DSN)

| Variable | Placeholder | Notes |
|---|---|---|
| `SENTRY_DSN` | *(blank)* | absent ⇒ Sentry fully disabled |
| `SENTRY_ENVIRONMENT` | `production` | |
| `SENTRY_RELEASE` | `viable-stone-backend@<git-sha>` | optional |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.05` | low |

`send_default_pii=False`; cookies, auth/CSRF headers, request bodies, customer
phones, payment references, push keys and offline tokens are scrubbed before
send; `request_id` is attached as a tag.

## Private storage

`DJANGO_STORAGE_BACKEND=local` keeps private media (`expenses/…`, `restocks/…`)
on the container filesystem — **acceptable for a first build only; a container
restart loses them.** For production choose a private S3-compatible bucket and
set:

| Variable | Placeholder | Notes |
|---|---|---|
| `DJANGO_STORAGE_BACKEND` | `s3` | |
| `AWS_STORAGE_BUCKET_NAME` | `viable-stone-private` | |
| `AWS_S3_ENDPOINT_URL` | `https://<provider-endpoint>` | | 
| `AWS_S3_ACCESS_KEY_ID` | `<key id>` | scoped to this bucket only |
| `AWS_S3_SECRET_ACCESS_KEY` | `<secret>` | |
| `AWS_S3_REGION_NAME` | `auto` / `us-east-1` | provider-dependent |
| `AWS_QUERYSTRING_EXPIRE` | `300` | signed-URL lifetime, seconds |

Objects are **private** (`AWS_DEFAULT_ACL=None`) and served via short-lived
signed URLs — never public-read. Requires `pip install "django-storages[s3]"`
(the `production` optional-dependencies extra). This project does **not** create
or choose a provider.

> **Railway Buckets caveat:** at time of writing Railway Buckets lack
> server-side encryption, object versioning and object-lock. Pick the final
> provider before production based on the shop's privacy and backup needs
> (e.g. Cloudflare R2 or Backblaze B2 with versioning + lifecycle rules).

## Never set

* `DEBUG=True` in production (startup validation rejects it).
* `CORS_ALLOW_ALL_ORIGINS` — always `False` in production settings.
* Any `DEMO_*` variable — the demo command refuses to run under production.
