# Viable Stone Backend — Implementation Progress

Specification: `2026-08-27-viable-stone-backend-blueprint.md` (Downloads folder).
This document tracks completed / active / pending phases. Updated as work proceeds.

Legend: ✅ done & verified · 🔄 active · ⏳ pending · ⚠️ blocked

## Verified starting state (2026-08-27)

- Python 3.13.7, Django 5.2.17, DRF 3.16.1, PostgreSQL 18.6 reachable, all deps installed.
- DB `viable_stone` / user `viable_stone_app` connect OK. No migrations applied.
- Redis not running locally — needed only for Stage 10 throttling/CI; configured with safe
  local-memory fallback so earlier phases are unblocked.
- Existing: partial `config/settings/*`, skeleton `apps/core` + `apps/accounts`, `.env` (untouched).

## Phases

| # | Phase | Status | Notes |
|---|-------|--------|-------|
| 1 | Inspect repo & report verified state | ✅ | See report in session + this file. |
| 2 | Settings / env / PG / DRF / CORS / spectacular config | ✅ | `check` + `check --deploy` clean, ruff clean. |
| 3 | Modular `apps` package structure | ✅ | All 7 apps present with api/services/tests subpackages. |
| 4 | Shared abstract `BaseModel` (UUID id, created_at, updated_at) | ✅ | `apps/core/models.py` (+ `UUIDModel`, `AuditLog`). |
| 5 | `Branch` + custom `User` before first migration | ✅ | + `RecoveryCode`, `RegisteredDevice`. Migration 0001 applied. |
| 6 | `AUTH_USER_MODEL` set before migrate | ✅ | `accounts.User`, set in base.py before any migration. |
| 7 | Auth, CSRF sessions, roles, branch scoping, MFA, recovery codes | ✅ | 22 auth/MFA/recovery/leak tests green. |
| 8 | Request IDs, API errors, pagination, permissions, audit, health, OpenAPI | ✅ | Error envelope + 401-vs-403 semantics verified; `openapi.yml` validates (0 warn/err). |
| 9 | Catalogue + price history | ✅ | 30 catalogue tests green — CI uniqueness, SKU/barcode, weighted price history, employee cost-free output. |
| 10 | Suppliers, restocking, balances, weighted-avg cost, movements | ✅ | 36 inventory tests green incl. 2 real-thread PostgreSQL concurrency tests, exact weighted-avg Decimal cases, atomic restock rollback, once-only confirm, opening-stock rules, stock-count apply. |

### Test run — 2026-08-27 (CREATEDB granted)

```
112 passed, 0 failed, 0 skipped   (pytest --create-db, PostgreSQL 18)
92% line coverage
ruff check .            -> All checks passed
ruff format --check .   -> clean
manage.py check         -> 0 issues
makemigrations --check  -> No changes detected
spectacular --validate  -> exit 0, 0 warnings, 0 errors (openapi.yml, 103 KB)
check --deploy (prod)   -> 0 issues
```

Fixes applied to get green: `ActivityTrackingMiddleware` used `type(lazy_user)` → now `get_user_model()`;
`exceptions.py` referenced non-existent `drf_exc.api_settings` → import from `rest_framework.settings`;
unauthenticated requests now 401 (`authenticate_header` on session auth) not 403;
disabled-account login now 403 `account_disabled` not 401; `PasswordChangeView` used `django_login`
without a backend → `update_session_auth_hash`; money `SerializerMethodField`s returned bare `Decimal`
(→ JSON float) → now `str()`; `OTP_TOTP_THROTTLE_FACTOR=0` in test settings; one test had wrong
arithmetic (160000 vs 166000).
| 11 | Ordinary fully-paid sales + split payments | ⏳ | |
| 12 | Receipt numbering + printable/PDF receipts | ⏳ | |
| 13 | Expenses + profit reports | ⏳ | |
| 14 | Discounts, approvals, rare returns, refunds, protected adjustments | ⏳ | |
| 15 | Notifications | ⏳ | |
| 16 | One-device offline fixed-price checkout + idempotent sync | ⏳ | |
| 17 | Security hardening, CI, monitoring, deployment, backup/restore docs | ⏳ | Redis required here. |
| 18 | Acceptance tests + final OpenAPI contract | ⏳ | |

## Recurring verification commands

```
.venv/Scripts/ruff check .
.venv/Scripts/python manage.py check
.venv/Scripts/python manage.py makemigrations --check --dry-run
.venv/Scripts/python -m pytest -q
.venv/Scripts/python manage.py spectacular --validate --file openapi.yml
.venv/Scripts/python manage.py check --deploy --settings=config.settings.production
.venv/Scripts/pip-audit
```

## Known environment blockers

- **pytest cannot create the test database.** The PostgreSQL role `viable_stone_app`
  owns `viable_stone` but lacks `CREATEDB`, so `pytest`/`manage.py test` fail with
  `permission denied to create database`. One-time fix, run as a PG superuser:

  ```
  ! psql -U postgres -c "ALTER ROLE viable_stone_app CREATEDB;"
  ```

  Until then, verification uses `ruff check`, `manage.py check`,
  `makemigrations --check`, and `spectacular --validate`. The full test suite is
  written alongside each phase and will be run once the grant is in place.
- **Redis not running locally** — `CACHE_BACKEND=locmem` fallback keeps dev working;
  needed for real for Stage 17 throttling/CI.

## Change log

- 2026-08-27: Repo inspected. Progress doc created.
- 2026-08-27: Stage 1 done — full `base.py` (DRF auth/pagination/throttle/exception
  handler, SPECTACULAR_SETTINGS, CACHES, otp/axes, logging, security cookies,
  storages), split dev/test/prod settings, ruff + pytest + coverage config in
  pyproject. `manage.py check` clean (dev + test), `ruff check .` clean.
- 2026-08-27: Stage 2 done — `apps` package fixed, 5 new apps created
  (catalog/inventory/sales/finance/notifications), `BaseModel`/`UUIDModel`/`AuditLog`
  in core, `Branch`/`User`/`RecoveryCode`/`RegisteredDevice` in accounts,
  `AUTH_USER_MODEL=accounts.User`, first migration generated + applied.
  Core infra: RequestID + activity middleware, request-id logging filter,
  private media storage, CSRF session auth, paginator, standard error envelope,
  axes lockout response. Accounts model tests written (23 cases) — pending CREATEDB.
