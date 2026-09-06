# Viable Stone Backend — Implementation Progress

Specification: `2026-08-27-viable-stone-backend-blueprint.md` (Downloads folder).
This document tracks completed / active / pending phases. Updated as work proceeds.

Legend: ✅ done & verified · 🔄 active · ⏳ pending · ⚠️ blocked

## Verified starting state (2026-08-27)

- Python 3.13.7, Django 5.2.17, DRF 3.16.1, PostgreSQL 18.6, all deps installed.
- DB `viable_stone` / user `viable_stone_app` connect OK. No migrations applied.
- Redis not running locally — `CACHE_BACKEND=locmem` fallback; real Redis only
  needed for Stage 17 (throttling/CI).
- Existing: partial `config/settings/*`, skeleton `apps/core` + `apps/accounts`,
  `.env` (untouched, git-ignored).

## Phases

| # | Phase | Status | Notes |
|---|-------|--------|-------|
| 1 | Inspect repo & report verified state | ✅ | |
| 2 | Settings / env / PG / DRF / CORS / spectacular config | ✅ | `check` + `check --deploy` clean. |
| 3 | Modular `apps` package structure | ✅ | 7 apps, each with `api/ services/ tests/`. |
| 4 | Shared abstract `BaseModel` (UUID id, created_at, updated_at) | ✅ | + `UUIDModel`, append-only `AuditLog`. |
| 5 | `Branch` + custom `User` before first migration | ✅ | + `RecoveryCode`, `RegisteredDevice`. |
| 6 | `AUTH_USER_MODEL` set before migrate | ✅ | `accounts.User`, pre-migration. |
| 7 | Auth, CSRF sessions, roles, branch scoping, MFA, recovery codes | ✅ | 15 auth-API + 7 model tests. |
| 8 | Request IDs, API errors, pagination, permissions, audit, health, OpenAPI | ✅ | 401-vs-403 semantics verified; `openapi.yml` validates. |
| 9 | Catalogue + price history | ✅ | 28 tests — CI uniqueness, SKU/barcode, price history, cost-free employee output. |
| 10 | Suppliers, restocking, balances, weighted-avg cost, movements | ✅ | 36 tests incl. 2 real-thread PostgreSQL concurrency tests. |
| 11 | Ordinary fully-paid sales + split payments | ✅ | Customer/ReceiptSequence/Sale/SaleItem/Payment; `create_sale` (11-step atomic, idempotent); cash/transfer/POS/split; per-branch-day receipt numbering; sales list + cashier "own sales" rule; customer API (phone masked in lists). 37 tests incl. 2 real-thread concurrency (idempotency + no oversell). |
| 12 | Receipt numbering + printable/PDF receipts | ✅ | A4 paid-receipt PDF (reportlab, no system deps) + JSON receipt, both from immutable snapshots. `GET /sales/{id}/receipt.pdf` (exact `Content-Type: application/pdf`, `Cache-Control: private, no-store`) + `/receipt/`, same role+branch scoping (cross-branch → 404). No cost/profit/DB-id/audit data. Currency shown as `NGN` (built-in fonts lack ₦; a non-Latin-1 symbol falls back). Business identity in `settings.BUSINESS_IDENTITY` only. Multi-page: header/columns repeat, "Page N" footer, long text wraps. 20 tests. |
| 13 | Expenses + profit reports | ✅ | `ExpenseCategory` (CI-unique per branch) + `Expense` (amount > 0 CHECK, `receipt_file`). `void_expense` service: owner-only, once-only, requires a reason, preserves the original amount, audited. Owner APIs `/expense-categories/`, `/expenses/` (+ `void` action, no delete → 405, amount immutable after create). `/reports/` (owner-only): `profit` (revenue = Σ completed-sale totals, COGS = Σ qty·unit_cost_snapshot, gross, net = gross − non-voided expenses; Africa/Lagos `today`/`week`/`month`/`custom` ranges, end-inclusive), `best-sellers`, `slow-movers`, `inventory` (stock value + low-stock). 24 tests (freezegun-dated). |
| 14 | Discounts, approvals, rare returns, refunds, protected adjustments | ✅ | **14A:** `ApprovalRequest`, `SaleReturn` (unique `(branch, client_return_id)`), `SaleReturnItem` (+`condition` RESELLABLE / DAMAGED_OR_OPENED), `Refund` (`issued_by`, amount>0). `submit_return_request` / `reject_return` / `approve_return` (owner-only, atomic, `select_for_update`, idempotent via `client_return_id`, ≤ sold−returned, original price+cost snapshots, RESELLABLE → `RETURN` movement + reverse COGS / DAMAGED → no restore, sale → PARTIALLY_RETURNED/RETURNED, receipt untouched). `adjust_stock` (owner, direction+positive qty+detailed reason, never negative, `select_for_update`, idempotent, audit before/after). `profit_report`/`best_sellers` net approved returns. **14B:** internal DRAFT sale (`create_draft_sale` — no payment/receipt/movement/report), `replace_draft_cart` (auto-supersedes a pending discount), `request_discount` (cashier, fixed Naira, reason, 0 < amount < subtotal), `approve_discount`/`reject_discount` (owner only), `finalise_draft` (atomic + idempotent + `select_for_update`; re-checks stock & active prices via a draft fingerprint → `approval_stale`; allocates the fixed discount across items in kobo by largest-remainder so parts sum exactly; cost snapshot taken at finalisation; payments == subtotal − discount; assigns receipt number). Discounted returns refund **net of discount** with a running kobo allocation (partial returns never over-refund; full return == final amount paid). Receipt shows Subtotal / Discount / Final total (PDF + JSON). APIs: `/sales/drafts/`, `/sales/{id}/{draft-cart,discount-requests,finalise,cancel}/`, polymorphic `/approvals/{id}/{approve,reject}/`, `/sales/{id}/return-requests/`, `/returns/`, `/inventory/adjustments/`. 83 tests incl. 3 real-thread PostgreSQL concurrency (approve-once, adjust-once, finalise-once). |
| 15 | Notifications | ✅ | Durable in-app `Notification` (recipient, branch, type, safe title/message, `is_read`/`read_at`, safe `related_object_type`/`id`, allowlisted `action_path`, `dedupe_key`; unique `(recipient, dedupe_key)`) + `PushSubscription` (unique `(user, endpoint)`, upsert, `failure_count`/`expired_at`). Every alert writes (and de-dupes) the durable row **inside the business transaction** — a rollback removes the change and the notification together, and a `(recipient, dedupe_key)` race is absorbed in a savepoint without poisoning that transaction; only the external Web Push is deferred to `transaction.on_commit` and a push failure never touches the business txn. **Central stock detection:** `write_movement` emits `stock_balance_changed` while the balance is locked; one receiver classifies OK/LOW/OUT vs `variant.low_stock_level` and alerts branch owners only on a level *change* into LOW or OUT — never repeatedly while low/out, direct drop to 0 sends only OUT_OF_STOCK, recovery resets the cycle, OUT→still-low sends one LOW_STOCK. Works identically through sales, returns, restocks, stock counts and protected adjustments. **Approvals:** submitting a discount/return request notifies active branch owners (not the requester); the decision notifies the requester (never self). APIs: `/notifications/` (list, `unread-count`, `{id}/read/`, `read-all/` — all own-only, cross-user/branch → 404, mark-read idempotent, no create/edit/delete), `/push-subscriptions/` (upsert-create, list, `{id}/deactivate/`, delete — own-only, HTTPS-or-localhost + key-length validation, rate-limited `notifications_write`), `/push-subscriptions/public-key/` (public VAPID key only). `p256dh`/`auth` never appear in any response body or `openapi.yml` response schema; private VAPID key is env-only, never in APIs/logs/openapi; `.env.example` uses illustrative placeholders only. 94 tests incl. real-thread PostgreSQL dedupe concurrency, in-transaction-row + rollback-removes-both, dedupe-race isolation, push-failure isolation, and protected-field-leak checks. |
| 16 | One-device offline fixed-price checkout + idempotent sync | ✅ | `accounts.OfflineDeviceAuthorization` binds one `RegisteredDevice` + branch + cashier + a versioned **signed** catalogue snapshot (active variants: name/SKU/fixed price/qty/low-stock — no cost, credentials, secret or customer data) + a ≤24h window (`OFFLINE_AUTHORIZATION_MAX_HOURS`). One ACTIVE per branch = the exclusive offline session: `assert_no_active_offline_session` makes online `create_sale`/`finalise_draft`/`adjust_stock`/`apply_stock_count` return `409 offline_session_active` until the owner ends it. Signing via `django.core.signing` (HMAC-SHA256 + `constant_time_compare`); `OFFLINE_SIGNING_KEY` env-only. **Sync** (`POST /offline/sync/`, device-bound by the signed token, cashier-auth): sales processed in device-sequence order, **every price/total recomputed from the signed snapshot** (client prices/totals/discounts/costs never trusted), each sale atomic, one outcome per sale — `ACCEPTED` / `DUPLICATE` / `CONFLICT` / `REJECTED` / `OWNER_REVIEW_REQUIRED` — in `sales.OfflineSaleSyncRecord` (unique `(branch, client_sale_id)` = idempotency + retry backstop). Stock conflicts and revoked/replaced/force-closed sessions are **retained** for owner review, never discarded, never negative, never partial. Accepted sales reuse `create_sale` with `source=OFFLINE` + `fixed_prices` + original `completed_at`, so they feed inventory / reports / receipts / notifications normally; official receipt number assigned on sync (temporary receipt is labelled `OFFLINE RECEIPT — PENDING SYNCHRONIZATION`, no number). Offline `Payment.offline_confirmed=True` (physically confirmed, not electronically verified); TRANSFER/POS require a reference. APIs: `/offline/devices/` (owner+MFA register/replace/revoke), `/offline/authorizations/` (owner+MFA issue/replace/revoke/`end-session` — force-end needs `mfa_confirmed`; `status/` gives expiry + pending count), `/offline/sync/`, `/offline/temporary-receipt/`, `/offline/sync-records/` (+ owner `resolve`), `/offline/sales/{client_sale_id}/` mapping. Cross-device/branch → 404; tampered/duplicate-sequence/outside-window payloads rejected; batch-size limit + `offline_sync` throttle; no token/phone/reference/secret in logs, responses or `openapi.yml`. 61 tests incl. 2 real-thread PostgreSQL concurrency (identical batches → one Sale; competing batches never oversell). |
| 17 | Security hardening, CI, monitoring, deployment, backup/restore docs | ✅ | **Nothing deployed.** `docker-compose.yml` (`redis:7.4.11-alpine` (digest-pinned), `127.0.0.1`-only, healthcheck) for local Redis; production `CACHES`/session/throttle/axes use Redis and **require `REDIS_URL`** (no fallback). Split probes: `/health/live/` (process) and `/health/ready/` (DB + Redis → 503 when down); bodies minimal. `config/settings/validation.py` fails startup if any required prod var is missing (`SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`, `CORS/CSRF_*`, `OFFLINE_SIGNING_KEY`, storage). Prod: `DEBUG=False`, secure cookies + HSTS (preload **off** until a real domain, `security.W021` silenced), exact CORS (`CORS_ALLOW_ALL_ORIGINS=False`), API schema/docs `IsOwnerOrTechAdmin` or disabled (`API_DOCS_ENABLED`). Expense uploads validated by **magic bytes** + size + extension-match (`apps/core/validators.py`), not extension alone. Optional Sentry (`apps/core/sentry.py`) — inert without `SENTRY_DSN`, `send_default_pii=False`, scrubs cookies/auth/CSRF headers, request bodies, phones, payment refs, push keys, offline tokens, DB URLs; keeps `request_id`. Structured JSON stdout logging (`LOG_FORMAT=json`, `JSONFormatter` scrubs secrets). Production `Dockerfile` (digest-pinned `python:3.13.15-slim-bookworm`, multi-stage, non-root `app` user, runtime deps only, `collectstatic` via throwaway `config.settings.build`, `HEALTHCHECK` → `/health/live/`, `gunicorn.conf.py` documents workers/threads/timeout/graceful-shutdown). `.github/workflows/ci.yml` — non-deploy, `permissions: contents: read`, cancel-superseded, Python 3.13 + PostgreSQL 18 + real Redis services, locked install (`requirements.lock`), ruff check/format, `manage.py check`, migration-drift, full pytest + real-Redis + **coverage ≥ 90**, spectacular `--fail-on-warn` + staleness, `check --deploy --fail-level WARNING`, `pip-audit`, secret-leak scan, Docker build + non-root smoke + readiness-503 + **Trivy HIGH/CRITICAL (fixed+unfixed, `ignore-unfixed:false`)** scan; every action pinned to a verified 40-hex commit SHA (`scripts/check_action_pins.sh`) + `dependabot.yml` (pip/docker/github-actions). Private storage: local dev on disk; prod prepped for generic S3-compatible (env-only, private ACL, signed URLs, `django-storages[s3]` extra) — no provider chosen/created. Docs: `BACKUP_RESTORE.md`, `FRONTEND_HANDOFF.md`, `MANUAL_TESTING.md`, `PRODUCTION_ENV.md` (placeholders only), `RAILWAY_SETUP.md`, `DEPLOYMENT.md`; `scripts/{backup_restore_drill,check_secrets,check_action_pins}.sh` (`check_secrets` allows only the exact CI placeholder DB URL). Dev-only `seed_demo` command (refuses under prod, idempotent, creds from env). 65 tests incl. a real `pg_dump`/`pg_restore` drill that verifies financial totals survive restore, and 5 real-Redis integration tests (run in CI). |
| 18 | Acceptance tests + final OpenAPI contract | ✅ | **Acceptance, security-audit & documentation stage — no new features.** 140 acceptance tests in `tests/acceptance/` across the 8 spec items: admin/debug policy (`/admin/` 404s under prod settings, no alternate URL, `DEBUG=False`, no debug/profiling app or route, docs gate unchanged, minimal health bodies); a reviewed **security classification for every `/api/v1/` route** (`security_classification.py` + `test_route_inventory.py` — build fails on an unclassified/stale route or a drifted public allowlist of 5); auth/authz matrix (401/403/404, MFA-required, CSRF on writes, disabled-user + revoked-device cutoff, no owner-only fields in employee output); rate-limiting (global anon/user baseline **added** — 120/min / 2000/min, overridable — plus per-scope throttles pinned per view, unthrottled health probes, safe 429 envelope, scrubbed `rate_limited` log, Redis in prod); input validation (unknown/computed fields silently ignored per a documented policy, NUL + C0/C1 control chars rejected via a new `ControlCharSafeSerializerMixin` on every write serializer, upload extension+MIME+magic+size, SQL-injection payloads inert, XSS/markup escaped for ReportLab via new `apps/core/text.py::pdf_escape` and verbatim in JSON — **no regex "sanitiser"**); **branch-isolation matrix** (`test_branch_isolation.py` — one row per branch-owned resource, outsider → 404 everywhere, coverage-of-coverage guard) with the **documented decision: no blanket PostgreSQL RLS** (isolation via permissions + branch querysets + locked services + DB constraints + these tests; prod DB role not SUPERUSER/CREATEROLE/CREATEDB/BYPASSRLS; RLS kept as a future defence-in-depth option); safe-errors + structured `apps.security` logging for auth-failure/axes-lockout/rate-limit/permission-denial/invalid-offline-signature/rejected-upload/500, all scrubbed, Sentry `scrub_event` still wired; 15 end-to-end business journeys on real PostgreSQL; **frozen OpenAPI contract** — `openapi.yml` regenerates byte-identical with zero warnings (99 paths / 139 ops / 160 schemas), path+method+operationId surface snapshotted in `tests/acceptance/openapi_contract_snapshot.json` (drift fails the build). **2 latent defects found & fixed:** `POST /products/` & `/variants/` 500'd because `record_audit` stored `UUID`s in a `JSONField` (fixed: `_json_safe` coercion in `apps/core/services/audit.py`); failed logins were not security-logged (fixed: scrubbed `auth_failed` line in `LoginView`, username+IP only). No protection reduced. New docs: `SECURITY.md`, `FINAL_ACCEPTANCE.md`; `FRONTEND_HANDOFF.md` gains money/date handling, the safe-text-rendering rule and the "fields the frontend must never compute or trust" table. Suite: **636 collected / 631 passed / 5 skipped** (5 = real-Redis, CI-only), coverage **93.6%**. Docker / Redis-CI / Trivy remain **unverified locally** — awaiting GitHub Actions. |

## Git checkpoints

- `985233f` — **Stages 1–10** (config, accounts+MFA, catalogue, inventory). 127 files.
- `7a04860` — docs: 112-test collection + estimate note.
- `27d9d2e` — **Stage 11** (fully-paid sales, split payments, receipt numbering).
- `29ef932` — **Stage 12** (A4 paid-receipt PDF + JSON receipt).
- `6f9c6b6` — **Stage 13** (expenses + profit reports).
- `1211ca0` — **Stage 14A** (returns, refunds, protected stock adjustments).
- `8243e7f` — **Stage 14B** — owner-approved fixed-Naira discount workflow
  (completes Stage 14).
- `2f64157` — **Stage 15** — notifications (durable in-app `Notification` +
  best-effort Web Push; central low/out-of-stock transition detection;
  approval-workflow alerts).
- `333806d` — **Stage 16** — one-device offline fixed-price checkout +
  idempotent sync (signed catalogue snapshot, exclusive session, device-bound
  batch sync with per-sale outcomes).
- **Stage 17** — production-readiness: Redis/Docker, security hardening, CI,
  monitoring, backup/restore + frontend/manual-testing docs. **Nothing
  deployed.**
- **Stage 18** — final acceptance tests + frozen OpenAPI contract:
  `tests/acceptance/` (140 tests, 8 spec items), `docs/SECURITY.md`,
  `docs/FINAL_ACCEPTANCE.md`, `tests/acceptance/openapi_contract_snapshot.json`;
  fixes: `record_audit` JSON-safety + `LoginView` auth-failure logging. Commit
  hash: _see the Stage 18 changelog entry below_. **Nothing deployed.**
- Branch `setup/backend-foundation`. `.env` excluded (git-ignored); `openapi.yml`
  and `docs/PROGRESS.md` committed in each.

## Test suite — 2026-08-27 (through Stage 17)

```
pytest --collect-only -q  -> 484 tests collected
pytest --create-db        -> 479 passed, 0 failed, 5 skipped   (PostgreSQL 18)
   (5 skipped = real-Redis integration; run in CI with the redis service)
coverage                  -> 92.95% lines  (CI gate: --cov-fail-under=90)
ruff check .              -> All checks passed
ruff format --check .     -> clean
manage.py check           -> 0 issues
makemigrations --check    -> No changes detected
spectacular --validate --fail-on-warn -> exit 0, 0 warnings, 0 errors (openapi.yml)
check --deploy (prod, --fail-level WARNING) -> 0 issues (1 silenced: W021 HSTS-preload, deliberate)
pip-audit                 -> No known vulnerabilities
secret-leak scan          -> OK
backup/restore drill      -> counts + financial totals identical after restore
```

### Collected test inventory (484, through Stage 17)

| File | Tests |
|---|---|
| accounts/tests/test_auth_api.py | 15 |
| accounts/tests/test_models.py | 22 |
| accounts/tests/test_users_api.py | 11 |
| catalog/tests/test_api.py | 12 |
| catalog/tests/test_models.py | 11 |
| catalog/tests/test_pricing.py | 5 |
| inventory/tests/test_adjustments.py | 9 |
| inventory/tests/test_api.py | 13 |
| inventory/tests/test_concurrency.py | 2 |
| inventory/tests/test_opening_stock.py | 4 |
| inventory/tests/test_restock.py | 5 |
| inventory/tests/test_stock_count.py | 5 |
| inventory/tests/test_weighted_cost.py | 7 |
| sales/tests/test_concurrency.py | 2 |
| sales/tests/test_create_sale.py | 19 |
| sales/tests/test_discounts.py | 24 |
| sales/tests/test_discounts_api.py | 13 |
| sales/tests/test_discounts_concurrency.py | 1 |
| sales/tests/test_receipts.py | 20 |
| sales/tests/test_returns.py | 18 |
| sales/tests/test_returns_api.py | 12 |
| sales/tests/test_returns_concurrency.py | 2 |
| sales/tests/test_sales_api.py | 16 |
| finance/tests/test_expenses.py | 14 |
| finance/tests/test_reports.py | 10 |
| finance/tests/test_reports_returns.py | 4 |
| notifications/tests/test_push_subscriptions_api.py | 19 |
| notifications/tests/test_stock_transitions.py | 17 |
| notifications/tests/test_notifications_api.py | 12 |
| notifications/tests/test_secret_hygiene.py | 10 |
| notifications/tests/test_stock_alerts.py | 8 |
| notifications/tests/test_approval_alerts.py | 7 |
| notifications/tests/test_models.py | 7 |
| notifications/tests/test_webpush.py | 5 |
| notifications/tests/test_transaction_boundary.py | 4 |
| notifications/tests/test_action_paths.py | 3 |
| notifications/tests/test_notifications_concurrency.py | 2 |
| sales/tests/test_offline_api.py | 16 |
| sales/tests/test_offline_sync.py | 16 |
| sales/tests/test_offline_authorization.py | 9 |
| sales/tests/test_offline_security.py | 7 |
| sales/tests/test_offline_snapshot.py | 6 |
| sales/tests/test_offline_models.py | 5 |
| sales/tests/test_offline_concurrency.py | 2 |
| core/tests/test_security_regression.py | 17 |
| core/tests/test_upload_validation.py | 14 |
| core/tests/test_health_probes.py | 5 |
| core/tests/test_redis_integration.py | 5 (skipped without Redis) |
| core/tests/test_sentry_scrub.py | 5 |
| core/tests/test_demo_data.py | 3 |
| core/tests/test_structured_logging.py | 3 |
| core/tests/test_backup_restore.py | 1 (skipped without pg tools) |
| **Total** | **484** (479 passed + 5 Redis-integration skipped locally) |

Stage 14 is complete: 14A (returns / refunds / protected adjustments) +
14B (owner-approved fixed-Naira discount on an internal DRAFT sale).
Stage 15 is complete: durable in-app notifications + best-effort Web Push,
central low/out-of-stock transition detection, approval-workflow alerts.
Stage 16 is complete: one authorised offline device per branch, a versioned
signed catalogue snapshot, an exclusive offline session, and a device-bound
idempotent batch sync that recomputes every price from the snapshot and
returns one outcome per sale.
Stage 17 is complete: the backend is production-ready but **not deployed** —
pinned Redis via Docker Compose, hard production config validation, secure
cookie/HSTS/CORS settings, gated API docs, magic-byte upload validation,
optional scrubbed Sentry, structured JSON logging, split liveness/readiness
probes, a non-root production Dockerfile + Gunicorn config, a non-deploy
GitHub Actions pipeline (PG 18 + real Redis, coverage ≥ 90, pip-audit,
secret scan, image smoke test), a verified local backup/restore drill, and
the frontend / manual-testing / backup / production-env / Railway docs.

(Stages 1–10 alone: 112.)

### Why the earlier Stage-10 estimate said ~124

The "~124" figure was an **informal running tally stated before the suite could
execute** (test DB was blocked on `CREATEDB`). It was approximate for concrete
reasons, and no tests were lost or skipped — `--collect-only` and the run both
report 112, 0 skipped:

1. **Parametrized cases were rounded loosely.** e.g. `test_weighted_cost.py`
   has one `@parametrize` block of 5 cases (5 collected items) plus a 1-case
   parametrize elsewhere; these were mentally counted as "a test" or "a few".
2. **Some cases were described then folded or deferred.** Several planned
   "no negative stock via a sale" assertions were moved into Stage 11 rather
   than written as stubs now; the concurrency file settled at 2 tests, not the
   3–4 first sketched.
3. **A throwaway debug test** (`test_debug_totp.py`, used to diagnose the
   django-otp throttle) briefly inflated a count that was later deleted.
4. **It was explicitly "~".** 112 is the measured, authoritative number from
   `pytest --collect-only -q`.

### Fixes applied to reach green (46 failures → 0)

- `ActivityTrackingMiddleware` used `type(lazy_user).objects` → `get_user_model()`.
- `exceptions.py` referenced non-existent `drf_exc.api_settings` → import from
  `rest_framework.settings`.
- Unauthenticated requests now return 401 (`authenticate_header` on session auth),
  403 reserved for authenticated-but-forbidden.
- Disabled-account login → 403 `account_disabled` (was 401).
- `PasswordChangeView` used `django_login` without a backend →
  `update_session_auth_hash`.
- Money `SerializerMethodField`s returned bare `Decimal` (JSON float) → `str()`.
- `OTP_TOTP_THROTTLE_FACTOR = 0` in **test** settings (device 1-second cooldown
  made a wrong-then-right assertion flaky).
- One test had wrong arithmetic (`160000` vs `100*1500 + 20*800 = 166000`).
- Added server-side `logger.exception` on unhandled 500s (client still gets only
  `{code: server_error, request_id}`).

## Recurring verification commands

```
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
.venv/Scripts/python manage.py check
.venv/Scripts/python manage.py makemigrations --check --dry-run
.venv/Scripts/python -m pytest -p no:randomly -q
.venv/Scripts/python manage.py spectacular --validate --file openapi.yml
.venv/Scripts/python manage.py check --deploy --settings=config.settings.production
.venv/Scripts/pip-audit
```

## Known environment notes

- **CREATEDB** — granted 2026-08-27 (`viable_stone_app` now has `rolcreatedb`).
  `pytest` creates/drops `test_viable_stone` freely; `--reuse-db` set in
  `pyproject.toml`.
- **Redis** not running locally — `CACHE_BACKEND=locmem` fallback keeps dev/tests
  working; real Redis needed for Stage 17.

## Change log

- 2026-08-27: Repo inspected; progress doc created.
- 2026-08-27: Stage 1 — full `base.py` (DRF auth/pagination/throttle/exception
  handler, SPECTACULAR_SETTINGS, CACHES, otp/axes, logging, security cookies,
  storages); split dev/test/prod; ruff + pytest + coverage in pyproject.
- 2026-08-27: Stage 2 — `apps` package fixed; 5 new apps; `BaseModel`/`UUIDModel`/
  `AuditLog`; `Branch`/`User`/`RecoveryCode`/`RegisteredDevice`; `AUTH_USER_MODEL`;
  first migration. Core infra: request-id + activity middleware, logging filter,
  private media storage, CSRF session auth, paginator, error envelope.
- 2026-08-27: Stages 3–10 — auth/MFA/recovery APIs; role/branch permissions +
  branch-scoped querysets; audit service + audit-log API; health + OpenAPI;
  catalogue (Category/Brand/Product/ProductVariant/PriceHistory) + `set_active_price`;
  inventory (Supplier/Restock/InventoryBalance/StockMovement/StockCount) +
  `open_stock`/`confirm_restock`/`apply_stock_count`, weighted-average costing,
  `select_for_update` locking, no-negative-stock.
- 2026-08-27: Test DB unblocked (CREATEDB). Full suite green: **112 passed**,
  92% coverage. Committed as `985233f` (+ `7a04860` docs).
- 2026-08-27: Stage 11 — sales + payments. `create_sale` service implements every
  blueprint rule inside one `transaction.atomic()`: active cashier/branch, merged
  cart lines, deterministic `select_for_update` balance locks, server-side prices
  (client price/total ignored), inactive/wrong-branch/insufficient-stock rejection,
  Decimal money, CASH/TRANSFER/POS/split payment validation with
  `sum(payments) == total` exactly, cost + name/price snapshots, one SALE movement
  per variant, per-branch-per-day receipt numbering, and an idempotency key
  (`client_sale_id`) so a retry never duplicates a sale or a stock reduction.
  Customer optional (walk-in creates no row; phone masked in broad lists).
  Full suite: **149 passed**. `openapi.yml` regenerated + validated (0 warn/err).
  Committed as `27d9d2e`.
- 2026-08-27: Stage 12 — A4 paid-receipt PDF. `reportlab` added (pure Python, no
  system libs). `apps/sales/services/receipts.py`: `receipt_context(sale)` (shared
  data) + `render_receipt_pdf(sale)`, both built only from `SaleItem`/`Payment`
  snapshots. Shows business identity (from `settings.BUSINESS_IDENTITY` — one
  place), optional logo, receipt number, Africa/Lagos date-time, branch, cashier,
  optional customer, per-line qty/unit price/line total, subtotal + total,
  Cash/Transfer/POS breakdown with tendered + change, and "COMPLETED / PAID".
  Never emits cost, profit, DB ids or audit data. `GET /api/v1/sales/{id}/receipt/`
  (JSON) and `/receipt.pdf` (explicit paths so `.pdf` isn't a DRF format suffix) —
  same `sales_visible_to` scoping, cross-branch → 404, unauthenticated → 401,
  non-completed sale → 409. Responses carry exact `Content-Type: application/pdf`,
  `Cache-Control: private, no-store`, `Content-Length`, `X-Content-Type-Options:
  nosniff` and `Content-Disposition: inline; filename="<receipt-no>.pdf"`.
  Currency = `NGN` (built-in PDF fonts have no ₦ glyph; a non-Latin-1
  `BUSINESS_CURRENCY_SYMBOL` is downgraded to `NGN` with a logged warning).
  Layout hardened for multi-page — `wordWrap="CJK"` cells (no clipping), items
  header `repeatRows=1`, "Page N" footer every page; verified by generating a
  representative 4-page receipt (real logo, 40 long-description lines, 3-way split
  payment, long branch name/address, customer) and inspecting the per-page
  extracted text. 20 tests incl. snapshot stability, logo embedding, exact
  Content-Type, Cache-Control, currency render+extract, ₦→NGN fallback and the
  multi-page structure. Full battery green: **169 passed**, 93% coverage,
  `openapi.yml` regenerated + validated (0 warn/err). `pip-audit`: only `pip`
  itself. `pypdf` added as a dev dep for PDF text assertions.
- 2026-08-27: Stage 12 committed as `29ef932`.
- 2026-08-27: Stage 13 — expenses + reports. `apps/finance`: `ExpenseCategory`
  (case-insensitive unique per branch) + `Expense` (amount > 0 CHECK,
  `receipt_file`, `is_voided`/`void_reason`/`voided_by`/`voided_at`).
  `void_expense` service — owner-only, `select_for_update`, once-only, requires a
  reason, never overwrites the amount, writes an audit row. Owner APIs
  `/api/v1/expense-categories/` and `/api/v1/expenses/` (`void` action; DELETE →
  405; `amount` immutable and edits blocked once voided). `/api/v1/reports/`
  (owner-only): `profit` (revenue = Σ completed-sale `total`; COGS =
  Σ `quantity * unit_cost_snapshot`; gross = revenue − COGS; net = gross −
  non-voided expenses; `period=today|week|month|custom`, Africa/Lagos bounds,
  end-date inclusive), `best-sellers`, `slow-movers` (in-stock, no sales in
  range), `inventory` (stock value + low-stock summary). 24 tests, freezegun for
  date ranges. Full battery: **193 passed**, 92% coverage; ruff / check /
  makemigrations --check / check --deploy / spectacular all clean.
- 2026-08-27: Stage 13 committed as `6f9c6b6`.
- 2026-08-27: Stage 14 — approvals, rare returns, refunds, protected stock
  adjustments. `apps/sales`: `ApprovalRequest` (generic, PENDING/APPROVED/
  REJECTED, immutable once decided), `SaleReturn` (unique `(branch,
  client_return_id)`), `SaleReturnItem` (+ `condition` RESELLABLE /
  DAMAGED_OR_OPENED), `Refund` (`issued_by`, amount > 0 CHECK). Services:
  `submit_return_request` (employee or owner, COMPLETED sale, qty ≤ sold —
  a proposal), `reject_return` (owner, no side effects, immutable), `approve_return`
  (owner only, one `transaction.atomic()`, `select_for_update(of=self)` on the
  request + `select_for_update` on sale items + `lock_balances` in sorted order,
  idempotent via `client_return_id`, hard cap = sold − already-returned, refund
  uses the original `unit_price_snapshot`, accounting uses the original
  `unit_cost_snapshot`, `sum(refunds) == return total` exactly, split refunds
  allowed; RESELLABLE → `RETURN` stock movement + COGS reversed, DAMAGED_OR_OPENED
  → no restore + COGS retained as loss; sale → PARTIALLY_RETURNED / RETURNED via
  the `force=True` save guard; the original paid receipt is never regenerated).
  `apps/inventory`: `adjust_stock` (owner only, INCREASE/DECREASE + positive whole
  qty + ≥10-char reason, never negative, `select_for_update` + immutable
  `ADJUSTMENT` movement, idempotency key re-checked under the row lock, audit
  records actor/branch/time/old→change→new). `profit_report` and `best_sellers`
  now net approved returns (revenue − return totals; COGS − reversed cost for
  RESELLABLE only; `returns_total` / `cost_reversed` exposed). APIs:
  `POST /api/v1/sales/{id}/return-requests/`, `/api/v1/approvals/` (+ owner-only
  `approve` / `reject`), read-only `/api/v1/returns/`, owner-only
  `POST /api/v1/inventory/adjustments/`. 45 tests incl. 2 real-thread PostgreSQL
  concurrency (parallel approvals apply once; parallel adjustments with the same
  key apply once). Full battery: **238 passed**, 92% coverage; ruff / check /
  makemigrations --check / check --deploy clean; `openapi.yml` regenerated +
  validated (0 warnings, 0 errors — payment-method / condition enums de-duped via
  shared `.choices`).
- 2026-08-27: **Stage 14A** committed as `1211ca0` (returns, refunds, protected
  stock adjustments). Stage 14 kept IN PROGRESS pending the discount workflow.
- 2026-08-27: **Stage 14B** — owner-approved fixed-Naira discount on an internal
  DRAFT sale. `apps/sales/services/discounts.py`: `create_draft_sale` (no
  payment / receipt / stock movement / report entry), `replace_draft_cart`
  (auto-supersedes a pending discount request → REJECTED), `request_discount`
  (cashier; fixed Naira; reason required; 0 < amount < subtotal), `approve_discount`
  / `reject_discount` (owner only; rejection has no side effects, reverts to
  DRAFT), `finalise_draft` (one `transaction.atomic()`, `select_for_update` on the
  sale, idempotent via a COMPLETED short-circuit; re-checks stock and current
  active prices against a draft **fingerprint** — cart edit or price change →
  `approval_stale`; `allocate_discount` splits the fixed discount across items in
  kobo by largest remainder so the parts sum exactly; cost snapshot taken at
  finalisation; payments == subtotal − approved discount; receipt number
  assigned). `returns.approve_return` now refunds the **net paid** amount for
  discounted lines via a running kobo allocation — partial returns never
  over-refund and a full return equals the final amount paid. The receipt
  (PDF + JSON) shows Subtotal / Owner-approved discount / Final total. APIs:
  `POST /api/v1/sales/drafts/`, `/sales/{id}/{draft-cart,discount-requests,finalise,cancel}/`,
  polymorphic `/api/v1/approvals/{id}/{approve,reject}/` (return or discount).
  `create_sale`'s helpers `merge_cart` / `validate_payments` /
  `next_receipt_number` promoted to public names and shared. 38 tests incl. a
  real-thread PostgreSQL concurrency test (two parallel finalisations → one
  completed sale, one payment, one SALE movement). Full battery: **276 passed**,
  93% coverage; ruff / check / makemigrations --check / check --deploy clean;
  `openapi.yml` regenerated + validated (0 warnings, 0 errors). Stage 14 complete.
- 2026-08-27: **Stage 14B** committed as `8243e7f`.
- 2026-08-27: **Stage 15** — notifications. New `apps/notifications`:
  `Notification` (durable in-app record — recipient, branch, type, safe
  title/message, `is_read`/`read_at`, safe `related_object_type`/`id`,
  allowlisted `action_path`, `dedupe_key`; unique `(recipient, dedupe_key)`) and
  `PushSubscription` (unique `(user, endpoint)`, upsert on register,
  `failure_count`/`expired_at`, auto-deactivate on 404/410 or repeated failure).
  `services/dispatch.py` writes (and de-duplicates) the durable row **inside the
  caller's transaction** — a rolled-back business event removes both the change
  and the notification; each `get_or_create` runs in its own savepoint so a
  `(recipient, dedupe_key)` race is absorbed without poisoning the surrounding
  transaction. Only the external Web Push is deferred with
  `transaction.on_commit`, and any push failure is swallowed so it can never
  affect the committed sale / movement / return / approval / adjustment.
  `services/webpush.py` makes no network call unless `PUSH_ENABLED` (false in
  tests / when VAPID keys absent) and logs only non-secret context (never the
  exception text, subscription keys or the VAPID private key). **Central stock
  detection:**
  `inventory.services.stock.write_movement` now emits a `stock_balance_changed`
  signal while the balance row is locked; `notifications/receivers.py` +
  `services/stock_alerts.py` classify OK/LOW/OUT against
  `ProductVariant.low_stock_level` and notify active branch owners only on a
  level *change* into LOW or OUT — no repeat while low/out, a direct drop to
  zero sends only OUT_OF_STOCK, recovery above threshold resets the cycle,
  OUT→still-low sends one LOW_STOCK. Identical behaviour across sales, returns,
  restocks, stock counts and protected adjustments. **Approval alerts:**
  `request_discount` / `submit_return_request` notify active branch owners
  (excluding the requester); `approve_*` / `reject_*` notify the original
  requester (never when requester == reviewer). APIs: `GET /notifications/`,
  `GET /notifications/unread-count/`, `POST /notifications/{id}/read/`,
  `POST /notifications/read-all/` (own-only, cross-user/branch → 404, mark-read
  idempotent, no client create/edit/delete); `POST|GET /push-subscriptions/`
  (upsert-create, list), `POST /push-subscriptions/{id}/deactivate/`,
  `DELETE /push-subscriptions/{id}/` (own-only; HTTPS-or-localhost + key-length
  validation; write endpoints rate-limited via `notifications_write` scope);
  `GET /push-subscriptions/public-key/` exposes only the browser-safe public
  key. `PushSubscriptionSerializer` never carries `p256dh` / `auth`; with
  `COMPONENT_SPLIT_REQUEST` those appear only in the `*Request` schema, so no
  response body or `openapi.yml` response component exposes them. VAPID private
  key is env-only — `.env.example` carries illustrative placeholders for the
  business-contact and VAPID lines (real values live only in `.env` /
  deployment env), `config/settings/base.py` defaults every contact and the
  VAPID private key to `""`. 94 tests incl. two real-thread PostgreSQL
  dedupe-concurrency tests, in-transaction-row + rollback-removes-both,
  dedupe-race-does-not-poison-transaction, push-failure isolation, and explicit
  protected-field-leak / no-secret-in-schema-openapi-logs checks. Full battery:
  **370 passed**, 93% coverage (apps/notifications 97%); ruff / check /
  makemigrations --check / check --deploy (prod) clean; `openapi.yml`
  regenerated + validated (`--fail-on-warn`, 0 warnings / 0 errors).
- 2026-08-27: **Stage 15** committed as `2f64157`.
- 2026-08-27: **Stage 16** — one-device offline fixed-price checkout + idempotent
  sync. New `accounts.OfflineDeviceAuthorization` (binds `RegisteredDevice` +
  branch + cashier + a versioned signed catalogue snapshot + a ≤24h window;
  one ACTIVE per branch) and `sales.OfflineSaleSyncRecord` (unique
  `(branch, client_sale_id)` idempotency/retry backstop; redacted payload for
  owner review). `sales.Payment` gains `offline_confirmed`. Snapshot build +
  signing in `sales/services/offline_snapshot.py` via `django.core.signing`
  (HMAC-SHA256 + `constant_time_compare`); the snapshot carries names / SKUs /
  fixed prices / quantities / low-stock levels only — never cost, credentials,
  the signing secret or customer data. `sales/services/offline.py`:
  `issue`/`replace`/`revoke` authorization, `end_offline_session`
  (force-end records FORCE_CLOSED + audit), and `sync_offline_batch` —
  sales processed in device-sequence order; every price and total recomputed
  from the signed snapshot (client prices / totals / discounts / costs never
  trusted); each sale atomic; one outcome per sale
  (ACCEPTED / DUPLICATE / CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED). Accepted
  sales reuse `create_sale` (new `fixed_prices` / `completed_at` /
  `business_date` / `payments_confirmed_offline` params, `source=OFFLINE`) so
  they feed inventory / reports / receipts / low-stock notifications normally;
  the official receipt number is assigned at sync, the on-device temporary
  receipt is labelled `OFFLINE RECEIPT — PENDING SYNCHRONIZATION` with no
  number. Stock conflicts and revoked/replaced/force-closed authorizations are
  retained for owner resolution — never discarded, never negative, never
  partial. **Exclusive session:** `accounts.services.offline_session.
  assert_no_active_offline_session` makes online `create_sale` /
  `finalise_draft` / `adjust_stock` / `apply_stock_count` return
  `409 offline_session_active` while a branch has an ACTIVE authorization.
  APIs under `/api/v1/offline/`: `devices/` (owner+MFA register / `replace` /
  `revoke`), `authorizations/` (owner+MFA create / `revoke` / `replace` /
  `end-session` — force-end requires `mfa_confirmed`; `status/` = expiry +
  pending count), `sync/` (device-bound by signed token, cashier-auth,
  `offline_sync` throttle, batch-size limit), `temporary-receipt/`,
  `sync-records/` (owner list + `resolve`), `sales/{client_sale_id}/`
  (official Sale + receipt mapping). Cross-device / cross-branch → 404;
  tampered / duplicate-sequence / outside-window payloads rejected;
  `OFFLINE_SIGNING_KEY` + `OFFLINE_SYNC_MAX_BATCH` env-only (`.env.example`
  placeholders); no signing secret, authorization token, payment reference or
  customer phone in logs, API responses or `openapi.yml`. 61 tests incl. 2
  real-thread PostgreSQL concurrency tests (identical batches → one Sale / one
  receipt; competing batches for scarce stock never oversell). Full battery:
  **431 passed** (PostgreSQL 18), 93% coverage; ruff / check /
  makemigrations --check / check --deploy (prod) clean; `openapi.yml`
  regenerated + validated (`--fail-on-warn`, 0 warnings / 0 errors).
- 2026-08-27: **Stage 16** committed as `333806d`.
- 2026-08-27: **Stage 17** — production-readiness (nothing deployed; no Railway /
  Sentry / storage / database resources created; no real secrets requested).
  **Redis:** `docker-compose.yml` with a pinned `redis:7.4.2-alpine`, bound to
  `127.0.0.1` only, `redis-cli ping` healthcheck; `config/settings/production.py`
  points `CACHES`, `cached_db` sessions, DRF throttling and django-axes at
  Redis and **requires `REDIS_URL`** (no locmem fallback); `REDIS_HEALTHCHECK`
  makes `/api/v1/health/ready/` ping Redis and 503 when it is down; 5 real-Redis
  integration tests (`@pytest.mark.redis`, skipped when unreachable, run by CI —
  no fakeredis). **Security hardening:** `config/settings/validation.py` fails
  startup if `SECRET_KEY` / `DATABASE_URL` / `REDIS_URL` / `ALLOWED_HOSTS` /
  `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` / `OFFLINE_SIGNING_KEY` /
  storage config is missing, or `DEBUG` is true; production enforces
  `DEBUG=False`, `Secure`/`SameSite` cookies, `SECURE_SSL_REDIRECT`, HSTS
  (preload deliberately **off** until a real domain — `security.W021`
  silenced), proxy SSL header, exact `CORS_ALLOWED_ORIGINS`
  (`CORS_ALLOW_ALL_ORIGINS=False`); API schema + Swagger/ReDoc are
  `IsOwnerOrTechAdmin`-only or fully disabled (`API_DOCS_ENABLED`).
  `apps/core/validators.py` validates expense-receipt uploads by size, real
  magic bytes and matching extension (a renamed script is rejected) — wired
  into `ExpenseSerializer`. New `apps/core/tests/test_security_regression.py`
  (17) and `test_upload_validation.py` (14). **Monitoring:** `apps/core/sentry.py`
  — Sentry is fully inert without `SENTRY_DSN`; when enabled,
  `send_default_pii=False`, `max_request_body_size="never"`, and `before_send`
  strips cookies, `Authorization` / `X-CSRFToken` headers, request bodies,
  customer phones, payment references, push keys, offline tokens and database
  URLs while keeping `request_id` as a tag. `apps/core/logging.py::JSONFormatter`
  emits one scrubbed JSON line per record on stdout (`LOG_FORMAT=json` in
  production). Split probes `/health/live/` (process) and `/health/ready/`
  (DB + Redis); both bodies minimal — no versions, credentials or addresses.
  **Private storage:** local disk for dev; production prepped for a generic
  private S3-compatible bucket entirely via `AWS_S3_*` env (private ACL,
  short-lived signed URLs, `django-storages[s3]` in a `production` extra) — no
  provider is chosen or created; the Railway Buckets encryption/versioning/lock
  gap is documented. **CI** (`.github/workflows/ci.yml`, non-deploy):
  `permissions: contents: read`, `concurrency` cancel-in-progress, Python 3.13,
  PostgreSQL 18 + `redis:7.4.2-alpine` service containers, install from
  `requirements.lock`, `ruff check` + `ruff format --check`, `manage.py check`,
  `makemigrations --check`, full pytest + real-Redis + `--cov-fail-under=90`,
  `spectacular --validate --fail-on-warn` + staleness check,
  `check --deploy --fail-level WARNING`, `pip-audit`, `scripts/check_secrets.sh`,
  and a Docker build + non-root-runtime smoke job; actions pinned to major tags
  with `.github/dependabot.yml` to SHA-pin and bump. **Production artifacts:**
  pinned `python:3.13.1-slim-bookworm` multi-stage `Dockerfile`, non-root `app`
  user, runtime deps only, build-time `collectstatic` via
  `config/settings/build.py`, `HEALTHCHECK` → `/health/live/`, `gunicorn.conf.py`
  (workers / threads / 30s timeout / 30s graceful shutdown / `max_requests`
  recycling); `.dockerignore`; no `railway.*` config-as-code — `docs/RAILWAY_SETUP.md`
  documents the dashboard steps and `docs/DEPLOYMENT.md` records migrations as a
  gated release step. **Backup/restore:** `docs/BACKUP_RESTORE.md` (local dump/
  restore, Railway volume snapshots + PITR, encrypted off-site `pg_dump`,
  retention, restore-into-a-new-DB, verification queries, drill procedure,
  RPO 1h / RTO 4h, Redis-is-disposable / PostgreSQL-is-authoritative);
  `scripts/backup_restore_drill.sh`; `apps/core/tests/test_backup_restore.py`
  dumps the live test DB, restores into a fresh scratch DB and asserts sale /
  payment counts and revenue / payment totals are identical.
  **Handoff:** `docs/FRONTEND_HANDOFF.md`, `docs/MANUAL_TESTING.md`,
  `docs/PRODUCTION_ENV.md` (placeholders only). Dev-only `seed_demo` management
  command — refuses under production settings, idempotent (`--reset` wipes the
  DEMO branch), credentials from `DEMO_*` env vars or `--interactive`, no
  committed passwords or real data. Full battery: **484 collected / 479 passed /
  5 skipped** (real-Redis, CI-only), **92.95%** coverage; ruff / check /
  makemigrations --check clean; `check --deploy --fail-level WARNING` clean
  (1 silenced: W021); `openapi.yml` regenerated + `--fail-on-warn` clean
  (+2 health paths); `pip-audit` clean; secret scan clean.
- 2026-08-27: **Stage 17 committed as `6e7f846`.**
- 2026-08-27: **Stage 17 follow-up — supply-chain & CI-verification hardening**
  (new commit on top of `6e7f846`; that commit was not amended). Base image
  moved to `python:3.13.15-slim-bookworm` pinned by its **verified**
  multi-arch manifest-list digest
  `sha256:c45a22ea…3b9ca` (resolved live from Docker Hub `library/python`,
  confirmed equal to `python:3.13-slim-bookworm`); Python **3.13** stays
  consistent across image / CI / `pyproject.toml`. Redis pinned to
  `redis:7.4.11-alpine@sha256:ff02b58f…eadf` (verified digest, equals
  `redis:7.4-alpine`) in `docker-compose.yml` and the CI service containers;
  PostgreSQL service pinned to `postgres:18@sha256:4ef4dbc9…2280`. Every
  GitHub Actions `uses:` is now a **full-length commit SHA** verified against
  the action's official repo, with the release tag in a comment:
  `actions/checkout@3d3c42e5…` (v7.0.1), `actions/setup-python@5fda3b95…`
  (v7.0.0), `aquasecurity/trivy-action@ed142fd0…` (v0.36.0). Dependabot keeps
  covering pip / docker / github-actions. **CI Redis verification corrected:**
  a "Wait for Redis" step, a dedicated `pytest -m redis` step that parses the
  JUnit XML and **fails if the five Redis tests are skipped / deselected / not
  collected** (expects exactly 5 passed, 0 skipped), the full-suite step now
  parses its JUnit XML and **fails on any skip** (expects ≥ 484 passed, 0
  skipped — achieved by also installing the PostgreSQL 18 client so the
  backup drill runs). **Docker job hardened:** builds the image, asserts the
  runtime UID ≠ 0, waits for `/health/live/` = 200, asserts `/health/ready/`
  = 200 with Postgres + Redis up and **= 503 when Redis is pointed at a dead
  port**, then a **Trivy** scan of the final image that fails on any fixable
  HIGH/CRITICAL OS or application vulnerability (`ignore-unfixed: true`
  documented; exceptions go in `.trivyignore`, currently empty).
  Locally re-verified (no Docker): 479 passed / 5 skipped (the 5 = real-Redis,
  which run in CI), 92.95% coverage, ruff clean, `manage.py check` +
  `makemigrations --check` clean, `check --deploy --fail-level WARNING` clean
  (1 silenced: W021), `openapi.yml` `--fail-on-warn` clean + no diff,
  `pip-audit` clean, secret scan clean, CI/compose/dependabot YAML valid.
  Docker build / runtime smoke / Trivy scan run only in GitHub Actions
  (`docker` is unavailable in the authoring shell). Nothing pushed or deployed.
- 2026-08-27: **Correct Stage 17 security scan enforcement** (new commit on top
  of `855833d`; both earlier commits unchanged). (1) Confirmed the
  `aquasecurity/trivy-action` reference in `ci.yml` is the **full 40-char SHA**
  `ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0` — only the earlier
  *report text* was abbreviated; the workflow was always correct. Added
  `scripts/check_action_pins.sh` (a CI step + tests) that **fails unless every
  `uses:` is exactly 40 hex chars**. (2) Trivy now runs with
  **`ignore-unfixed: false`** — it reports and fails on **every** HIGH/CRITICAL
  OS/application vulnerability, fixed *or* unfixed. `.trivyignore` rewritten:
  exclusions must be specific CVE ids each carrying CVE id + package + reason +
  residual-risk assessment + approver/date + review-by date; **no approved
  exclusions exist** and none was added to make CI pass (if the pinned base
  ships an unpatched HIGH/CRITICAL, the job fails until the digest is bumped).
  (3) `scripts/check_secrets.sh` no longer exempts inline DB credentials by
  hostname — a real-looking `postgres://user:pass@localhost/...` is now
  rejected. Only the two **exact** documented CI placeholder URLs
  (`postgres://viable:viable@{localhost,127.0.0.1}:5432/viable_ci`) are
  accepted; a `SECRET_SCAN_PATHS` override makes it unit-testable, and the PEM
  pattern is now passed with `grep -e` (a leading `-` was silently disabling
  that rule). New `apps/core/tests/test_supply_chain_scans.py` (12) proves:
  remote inline cred → rejected, localhost inline cred → rejected, near-miss of
  the placeholder → rejected, only the exact placeholder → accepted, PEM key →
  caught, `.env` untracked + `.gitignore` has `.env` and `!.env.example`, the
  pin audit passes on the real workflows and fails on a tag / short SHA, and
  each of the three `ci.yml` `uses:` lines carries a full 40-hex SHA + version
  comment. Local re-verify (no Docker): **496 collected / 491 passed / 5
  skipped** (5 = real-Redis, CI-only), 92.88% coverage; ruff / `manage.py
  check` / `makemigrations --check` clean; `check --deploy --fail-level
  WARNING` clean (1 silenced: W021); `openapi.yml` `--fail-on-warn` clean, no
  diff; `pip-audit --strict --skip-editable` clean; secret scan + regression
  suite pass; action-pin audit passes; CI/compose/dependabot YAML valid.
  Trivy scan + full Docker job run only in GitHub Actions. Nothing pushed.
- 2026-08-27: **Stage 18** — final acceptance tests + frozen OpenAPI contract.
  Acceptance/security-audit/documentation stage; **no new features, no
  protection reduced.** New `tests/acceptance/` package (140 tests) across the
  8 spec items — see the Phase 18 row for the breakdown. Added a global DRF
  throttle **baseline** (`AnonRateThrottle` 120/min + `UserRateThrottle`
  2000/min, both env-overridable) alongside the existing per-scope throttles;
  `ControlCharSafeSerializerMixin` (new `apps/core/serializers.py`) on every
  write serializer rejects C0/C1 control characters (NUL already rejected by
  DRF; tab/newline allowed); new `apps/core/text.py::pdf_escape` escapes
  user/owner text before it reaches ReportLab's `Paragraph`; `/admin/` is now
  behind `ADMIN_ENABLED` (a conditional URLconf include) and 404s under
  production settings with no alternate URL. **2 latent defects found & fixed:**
  (1) `POST /api/v1/products/` and `/api/v1/variants/` returned **500** —
  `record_audit` stored serializer `UUID`s in a `JSONField`; fixed with a
  `_json_safe` coercion in `apps/core/services/audit.py`. (2) Failed logins
  were not written to the security log; added a scrubbed `auth_failed`
  `apps.security` line in `LoginView` (username + client IP + path, never the
  password). **Decision recorded:** no blanket PostgreSQL RLS at Stage 18 —
  branch isolation stays enforced by permissions + branch-scoped querysets +
  locked services + DB constraints + the `test_branch_isolation.py` matrix;
  RLS kept as a documented future defence-in-depth option; production DB role
  must not be `SUPERUSER`/`CREATEROLE`/`CREATEDB`/`BYPASSRLS`. **OpenAPI
  frozen:** `openapi.yml` regenerates **byte-identical** with **zero warnings**
  (99 paths / 139 operations / 160 schemas); the path+method+operationId
  surface is snapshotted in `tests/acceptance/openapi_contract_snapshot.json`
  and `test_openapi_contract.py` fails on any un-reviewed drift. New docs:
  `docs/SECURITY.md`, `docs/FINAL_ACCEPTANCE.md`; `docs/FRONTEND_HANDOFF.md`
  gains money/date handling, the safe-text-rendering (XSS) rule and the
  "fields the frontend must never compute or trust" table. Local verify
  (no Docker, no local Redis): **636 collected / 631 passed / 5 skipped**
  (5 = real-Redis, CI-only), coverage **93.6%**; `ruff check` + `ruff format
  --check` clean; `manage.py check` + `makemigrations --check` clean;
  `check --deploy --fail-level WARNING` under production settings clean
  (1 silenced: W021); `spectacular --validate --fail-on-warn` exit 0, no diff;
  `pip-audit --strict --skip-editable` clean; secret scan + action-pin audit
  pass; workflow/compose YAML valid. **Awaiting GitHub Actions:** full suite
  with 0 skips, the 5 real-Redis tests, Docker build + non-root smoke +
  readiness-503, Trivy HIGH/CRITICAL. Committed as `Stage 18: final acceptance
  tests and OpenAPI contract`. **Nothing pushed, no remote created, not
  deployed, frontend not started.**

- 2026-08-28: **Post-Stage-18 correction — secure product-image delivery + wider
  catalogue search** (justified additive change; strict TDD, failing tests
  first). *Root cause:* a product image uploaded fine and lived in private
  storage, but nothing served it — `MEDIA_URL = /api/v1/files/` has **no route
  and no view**, so `image.url` (and the serializer's `image` field) resolved
  to a dead path. A blanket media route was rejected on purpose:
  `PRIVATE_MEDIA_ROOT` also holds expense receipts.
  *Fix:* new authenticated, branch-scoped endpoint
  **`GET/DELETE /api/v1/products/{id}/image/`** (`apps/catalog/api/urls.py` +
  `ProductViewSet.image` / `image_clear`). GET = owner or a branch employee;
  DELETE = owner only (204 / 403 / 404). Bytes stream via `storage.open` only
  (never `.path`), so local `PrivateMediaStorage` and a future private S3
  bucket work unchanged; response carries the real image `Content-Type`,
  `X-Content-Type-Options: nosniff`, `Cache-Control: private, max-age=300` and
  a bare `filename=` (no path/key leak). New additive read field
  **`image_url`** (absolute URL, nullable) on product responses; the binary
  `image` multipart request field is unchanged. New
  `apps/catalog/validators.py::validate_product_image` — signature-checked
  **JPEG/PNG/WebP only, 5 MB** (`PRODUCT_IMAGE_MAX_BYTES`), extension must
  match real content; a disguised PDF is rejected. Deliberately **not** the
  receipt validator (that permits PDF). **Search:** `variants/?search=` now
  also matches product name, colour, size, finish, brand name and category
  name (was SKU/barcode/product-name); branch isolation + employee active-only
  visibility unchanged. *Contract:* `openapi.yml` regenerated byte-stable,
  `--fail-on-warn` clean (**100 paths / 141 operations**); snapshot
  `tests/acceptance/openapi_contract_snapshot.json`, `security_classification
.py` (`product-image`) and the `test_branch_isolation.py` matrix updated in
  step; `docs/FRONTEND_HANDOFF.md` gains a **Product images** section. No
  model change, no migration. Local verify: **670 passed / 5 skipped**
  (5 = real-Redis, CI-only), coverage **94%**; `ruff check` + `ruff format
  --check` clean; `manage.py check` + `makemigrations --check` clean;
  `check --deploy --fail-level WARNING` (production) clean (1 silenced: W021);
  `spectacular --validate --fail-on-warn` exit 0. 39 new tests
  (`apps/catalog/tests/test_product_images.py` 28,
  `apps/catalog/tests/test_variant_search.py` 11). **Not committed, not pushed,
  not deployed.**

- 2026-08-28: **Frontend-integration corrections G19 + G16** (strict TDD;
  failing tests first). **G19 — draft restock total:** `POST /api/v1/restocks/`
  computed each `RestockItem.line_total` but never set the parent
  `Restock.total_cost`, so a retrieved DRAFT returned `total_cost: "0.00"`
  while its lines summed to the real purchase amount. `total_cost` was only
  written by `confirm_restock`. Blueprint says `total_cost` is
  "calculated by server" with no status qualifier and `line_total` is
  "server calculated" — so `total_cost` is the **purchase total in every
  state**, not zero-until-confirmed. *Fix:* new single source of truth
  `apps/inventory/services/restock.py::restock_purchase_total(restock)` — exact
  Decimal `Σ quantity × unit_cost`, rounded to kobo. `RestockWriteSerializer
  .create` now sets it right after `bulk_create`; `RestockViewSet
  .perform_update` keeps it consistent on a metadata PATCH; `confirm_restock`
  now takes its `total_cost` from the same helper. **Adjacent bug fixed:**
  `RestockWriteSerializer.validate()` did unconditional `attrs["supplier"]` /
  `attrs["items"]` and 500'd on any partial update of a draft — now uses
  `.get()`. No model / schema / migration change (`total_cost` field already
  existed). 12 new tests (`test_api.py::TestDraftRestockTotal` 6,
  `test_restock.py::TestRestockPurchaseTotal` 6). **G16 — polymorphic approval
  contract:** `POST /api/v1/approvals/{id}/approve/` dispatches at runtime on
  `approval.request_type` (DISCOUNT → body `{amount, reviewer_note?}` → returns
  `ApprovalRequest`; RETURN → body `{lines[], refunds[], reviewer_note?,
  client_return_id?}` → returns `SaleReturn`), but `@extend_schema` hard-coded
  `request=ApproveReturnSerializer` + `responses={200: SaleReturnRead}` — so
  OpenAPI told clients to always send the return body for every type. **Runtime
  is correct; only the published schema was wrong** → schema + docs only.
  `approve`'s `@extend_schema` now uses `PolymorphicProxySerializer`: request
  `ApproveRequestRequest` = `oneOf[ApproveReturnRequest, ApproveDiscountRequest]`
  (new `ApproveDiscountRequest` component, `amount` required); `200`
  `ApproveResult` = `oneOf[SaleReturnRead, ApprovalRequest]`; description
  documents dispatch on the read-only `request_type` enum already on
  list/detail. `reject` was already accurate (`{reviewer_note?}` → 
  `ApprovalRequest` for both) — unchanged. Owner-only + MFA + branch-scoped
  404 already enforced (`IsOwner` + branch-scoped queryset). Wrong-body errors
  name the missing field (`amount`, or `lines`/`refunds`). 13 new tests
  (`apps/sales/tests/test_approvals_contract.py`) — runtime shapes both ways,
  authz/MFA/branch, and OpenAPI `oneOf` assertions. `openapi.yml` regenerated,
  `--fail-on-warn` exit 0; path/operationId surface unchanged (contract
  snapshot unaffected). `docs/FRONTEND_HANDOFF.md` gains an **Approvals**
  section with the exact payload per type. **Not committed, not pushed, not
  deployed.**

- 2026-08-29: **Auth hardening — failed sign-in must not preserve a prior
  session.** A manual tester reported "logged in with wrong credentials". The
  credential checks are all correct (13 new tests in
  `apps/accounts/tests/test_wrong_credentials.py`, django-axes on, prove it:
  wrong password / unknown user → `401` no session; MFA owner with the right
  password is `403` on `/me/` and every business endpoint until a **valid**
  TOTP or recovery code clears MFA; wrong TOTP / wrong recovery / no device /
  no codes → `400`, session stays unverified; the right code afterward still
  works). Root cause of the report: `POST /api/v1/auth/login/` has
  `authentication_classes = []`, so a *failed* attempt returned `401` while
  leaving any **pre-existing** authenticated (and MFA-cleared) browser session
  fully usable — the app kept working off the old `vs_sessionid`. *Fix
  (`apps/accounts/api/auth_views.py`):* after body validation, if the request
  carries an auth session (`_auth_user_id`), `request.session.flush()` before
  authenticating — a sign-in attempt always starts fresh; a successful login
  re-establishes it via `django_login`. A malformed (`400`) body is not a
  credential attempt and keeps the session. No API/schema/model/migration
  change. `docs/FRONTEND_HANDOFF.md` Auth section reworked: gate on
  `mfa_verified === true` (not the `200`), treat any non-2xx from login as
  signed-out, stay on the MFA screen on a `400`. Full suite **740 passed / 5
  skipped**; ruff / `manage.py check` / migrations / `check --deploy` /
  `spectacular --fail-on-warn` all clean. **Not committed, not pushed.**

- 2026-08-29: **Real server-side product filtering.** `GET /api/v1/products/`
  silently ignored `?category=` / `?brand=` — the endpoint carried only
  `SearchFilter` + `OrderingFilter`, and DRF filter backends drop query params
  they do not own. New `apps/catalog/api/filters.py::ProductFilterBackend`
  (added to `ProductViewSet.filter_backends` before search/ordering): a small
  DRF Serializer validates `category` (uuid), `brand` (uuid), `kind`
  (`PAINT` | `EQUIPMENT`, the real model field name) and `is_active` (bool);
  invalid values raise `ValidationError` → the standard `validation_error`
  envelope (every bad param in `field_errors`), never a 500 or a silent
  ignore. Applied as exact `category_id` / `brand_id` / `kind` / `is_active`
  filters, AND-combined, only on the `list` action, on top of the queryset the
  view has already branch- and role-scoped — so branch isolation holds (a
  foreign category/brand id → empty page) and an employee's `is_active=false`
  just AND's with the forced `is_active=True` → empty page, never an inactive
  product. Gate is on the raw query key, not `validated_data`: DRF
  `BooleanField` reads a QueryDict as HTML input, so an *absent* `is_active`
  resolves to `False` — reading it unconditionally would have filtered every
  request to inactive-only. Composes with `search` / `ordering` / `page` /
  `page_size`; response shape unchanged (`PaginatedProductReadList`), no
  cost/owner-only fields added. `openapi.yml` regenerated — `products_list`
  now documents `category` / `brand` / `kind` / `is_active` (drf-spectacular
  picks up `get_schema_operation_parameters`); `--fail-on-warn` clean;
  path/operationId surface unchanged (contract snapshot unaffected).
  `docs/FRONTEND_HANDOFF.md` gains a **Product list** filter table. 23 tests
  in `apps/catalog/tests/test_product_filters.py` (each filter alone, combined,
  search+filter, ordering+pagination, empty result, invalid values, unknown
  id, cross-branch isolation, employee visibility, no cost leak). **Not
  committed, not pushed.**

- 2026-08-30: **Frontend gap G2 — offline device can now READ the signed
  catalogue snapshot** (strict TDD; failing tests first; additive; nothing
  committed/pushed/deployed). *Root cause:* Stage 16 **builds, signs and
  stores** the fixed catalogue snapshot (`OfflineDeviceAuthorization.snapshot`
  JSON + the same rows inside `signed_token`) and **consumes** it server-side
  in `/offline/sync/` and `/offline/temporary-receipt/`, but exposed **no read
  contract** — `OfflineAuthorizationSerializer` deliberately returns only
  `snapshot_version` (a number), not the rows. A disconnected till therefore
  had no way to *build* an offline sale (fixed prices, quantity-at-open,
  low-stock label). No second offline protocol was needed; only a projection
  of the snapshot that already exists. *Fix — one narrowly-scoped additive
  endpoint:* **`GET /api/v1/offline/authorizations/{id}/snapshot/`**
  (`OfflineAuthorizationViewSet.snapshot` action + new
  `offline.catalogue_snapshot_for_cashier` service). Chosen over widening the
  create/status responses so the large fixed set never bloats list/status,
  the response shape of existing endpoints is untouched, and retrieval binds
  to the **single cashier** (not any branch user). Explicit serializers
  `OfflineCatalogueSnapshotSerializer` / `OfflineCatalogueSnapshotItemSerializer`.
  Response: `authorization_id`, `status` (always `ACTIVE` on 200),
  `snapshot_version`, `issued_at`, `expires_at`, `signed_token` (the existing
  Stage 16 proof, echoed for `authorization_token`), and `items[]` with the
  **exact** keys `variant_id` / `name` / `sku` / `price` (Decimal string, 2 dp)
  / `quantity` (whole units at session open) / `low_stock_level` — the same
  field names the sync service already keys on, so no translation layer.
  `Cache-Control: private, no-store`. Not paginated (bounded per-branch active
  set). *Security:* reuses `load_authorization_for_token` — same constant-time
  `django.core.signing` verification and branch/device/cashier/token binding as
  `/offline/sync/`; wrong cashier / device / branch → an **indistinguishable
  404**; a non-ACTIVE session → `409 offline_session_not_active`, expired →
  `409 offline_authorization_expired` (standard error envelope); revoked device
  → 404; the payload is served from the **verified signed token**, so it is
  frozen for the session (repeated reads byte-identical, a later online price/
  stock change never leaks in, and a mutated `snapshot` DB column is ignored).
  No cost / average cost / stock value / profit / credential / signing key /
  customer data in the body or logs; signature verification and constant-time
  compare unchanged; one-active-device-per-branch and the online-checkout lock
  untouched. *Contract:* `openapi.yml` regenerated, `--fail-on-warn` clean,
  byte-stable (**101 paths / 142 operations**); `openapi_contract_snapshot.json`
  (+1 path, +1 operationId), `security_classification.py`
  (`offline-authorization-snapshot`) and `test_branch_isolation.py`
  `indirectly_covered` updated in step. `docs/FRONTEND_HANDOFF.md` Offline
  section reworked (new step 3 with the exact JSON + field-name callout; header
  count 101/142). 24 new tests in
  `apps/sales/tests/test_offline_snapshot_read.py` (authorised success, exact
  Decimal price + whole-unit qty, snapshot stability under live price/stock
  change, repeated retrieval, mutated-column ignored, no cost/secret leakage,
  wrong cashier/device/branch = indistinguishable 404, unauthenticated 401,
  expired/revoked/replaced/force-closed envelopes, tampered-token/body-edit
  rejection, sync-service price parity, OpenAPI secret hygiene). No model
  change, no migration. Local verify: full suite green (5 skipped = real-Redis
  / pg-drill, CI-only); `ruff check` + `ruff format --check` clean;
  `manage.py check` + `makemigrations --check` clean;
  `check --deploy --fail-level WARNING` (production) clean (1 silenced: W021);
  `spectacular --validate --fail-on-warn` exit 0, no diff. Live authenticated
  browser test deliberately **not** run — **AWAITING USER MANUAL
  VERIFICATION**. **Not committed, not pushed, not deployed.**

- 2026-08-30: **Frontend contract-gap audit G1–G22** (strict TDD; failing tests
  first; additive only; **nothing committed / pushed / deployed**). Verified
  every G1–G22 claim against implementation / serializers / permissions /
  filters / services / tests / generated OpenAPI. No path, method or
  operationId changed; the frozen contract snapshot is unchanged except for the
  G2 snapshot endpoint already recorded above. Runtime behaviour changed only
  where a filter was previously **silently ignored** (now a `400
  validation_error`).
  * **G22 — `SaleRead.discount_request`** (new nullable read-only
    `SaleDiscountRequestSummary`, drf-spectacular `ENUM_NAME_OVERRIDES` pins
    `ApprovalStatusEnum` so `status` reuses the canonical enum). Populated from
    the newest DISCOUNT `ApprovalRequest`; `requested_amount` /
    `approved_amount` from `requested_changes`; `reviewed_by_username` /
    `requested_by_username` only (no raw ids, no `fingerprint`, no `subtotal`,
    no cost). `sales_visible_to` gains a `Prefetch(to_attr="_discount_requests",
    ...select_related requested_by/reviewed_by)` so a sale list is not N+1.
    Visible to exactly the users who can already GET the sale. Documented
    lifecycle: **approve does NOT move `Sale.status`** (stays
    `PENDING_APPROVAL`; `discount_total` becomes the approved amount);
    **reject reverts to `DRAFT`, `discount_total` `"0.00"`**; a cart edit /
    cancel supersedes a pending request into a `REJECTED` row with
    `reviewed_by_username: null` + `reviewer_note` `"Superseded: …"` (the model
    has no `CANCELLED`/`EXPIRED`). 22 tests
    (`apps/sales/tests/test_sale_discount_request_read.py`).
  * **G21 — `GET /approvals/` `?status` / `?request_type`** — new
    `apps/sales/api/list_filters.py::ApprovalFilterBackend` (same validated
    `BaseFilterBackend` + serializer pattern as `ProductFilterBackend`), wired
    with `SearchFilter`/`OrderingFilter`; invalid value → `validation_error`;
    branch + cashier scoping preserved. `test_approvals_filters.py` (13).
  * **G3 — `GET /offline/sync-records/` `?outcome` / `?resolved`** — the
    viewset already applied these ad-hoc; replaced with
    `OfflineSyncRecordFilterBackend` (validated + documented; owner-only +
    branch scoping unchanged). `test_offline_sync_records_filters.py` (10).
  * **G18 / G13 — reports** — `ReportsViewSet.pagination_class = None`;
    `best-sellers` / `slow-movers` now generate as **bare arrays** (they never
    paginated); `?limit` 1..100 (default 10 / 20) is the only row cap.
    operationIds unchanged (`…_list` kept). `test_openapi_contract.py`'s
    paginated-wrapper check gains a documented two-entry exemption.
    `apps/finance/tests/test_report_contract.py` (7).
  * **G4 / G5 — inventory schemas** (runtime unchanged): `inventory/movements/`
    now documents its **required `?variant=`** param + `page`/`page_size` and a
    **paginated `StockMovement`** response (was `parameters=[]` + a single
    `InventoryBalanceEmployee`); `inventory/low-stock/` is a paginated list;
    `inventory/stock-value/` uses a new `StockValue` summary schema
    (`{stock_value}`), owner-only. operationIds kept explicit.
    `apps/inventory/tests/test_inventory_contract.py` (9).
  * **G9 — `DraftCreateRequest.customer`** typed as `CustomerInlineRequest`
    (was free-form `object`); omitted / `null` → walk-in unchanged.
    `apps/sales/tests/test_draft_customer_contract.py` (4).
  * **G1 — error envelope** — new `apps/core/openapi.py::add_error_envelope`
    post-processing hook publishes an `Error` schema + `components.responses.
    Error` and attaches it as the `default` response of every operation; the
    stable `code` catalogue is documented in `FRONTEND_HANDOFF.md`. No
    per-endpoint error invention. `tests/acceptance/test_error_contract.py` (5).
  * **G2** — reported `ALREADY_IMPLEMENTED` this session; docs updated to
    resolve the 404-vs-409 split (identity/existence mismatch = blind 404;
    a dead-but-provably-yours session = 409 envelope) and to spell out the
    server-side device/cashier/branch binding proof.
  * **G6 / G7 / G8 / G10 / G11 / G12 / G14 / G15** — no new backend feature;
    verified + documented (branch timezone read path, customer phone-masking
    matrix, `client_finalize_id` idempotency, MFA-state-from-`LoginResponse`).
  * **G16 / G17 / G19 / G20** — `ALREADY_IMPLEMENTED`; their suites re-run
    green and OpenAPI still documents them.
  Files: `apps/sales/api/{serializers,discount_serializers,list_filters,
  return_views,offline_views}.py`, `apps/sales/selectors.py`,
  `apps/finance/api/views.py`, `apps/inventory/api/{views,serializers}.py`,
  `apps/core/openapi.py` (new), `config/settings/base.py`
  (`ENUM_NAME_OVERRIDES` + `POSTPROCESSING_HOOKS`), `openapi.yml`,
  `tests/acceptance/{test_openapi_contract.py,openapi_contract_snapshot.json}`,
  `docs/FRONTEND_HANDOFF.md`. **No model change, no migration.** Local verify
  results recorded in the audit report. Docker / Redis-CI / Trivy remain
  external (not run locally). **Not committed, not pushed, not deployed.**

- 2026-08-31: **Offline sync follow-ups G23 + review-queue completeness +
  detail_code catalogue** (strict TDD; failing tests first; additive;
  **nothing committed / pushed / deployed**).
  * **G23 — `POST /offline/sync/` response shape.** The runtime has always
    returned one object `{ "results": [...] }`, but `@extend_schema` declared
    `OfflineSyncResultSerializer(many=True)` → drf-spectacular rendered a bare
    `OfflineSyncResult[]`. Fixed the schema to match: new
    `OfflineSyncResponseSerializer` (`results = OfflineSyncResultSerializer
    (many=True)`), and the view now serialises through it. Runtime unchanged;
    every existing test that reads `res.json()["results"]` still passes.
    operationId `offline_sync_create` unchanged (contract snapshot unaffected).
  * **Review queue includes unresolved `REJECTED`.** New module constant
    `_REVIEW_QUEUE_OUTCOMES = (CONFLICT, OWNER_REVIEW_REQUIRED, REJECTED)` used
    by `pending_review_count`, `_flag_pending_for_review` and
    `resolve_sync_record`. Rationale: a `REJECTED` offline sale is a real
    transaction the device already took money for — it must show in the
    owner's queue, block a plain `end-session`, and be resolvable (owner
    reconciles it, e.g. re-rings online, then marks the record resolved). No
    schema/model change; `resolve_sync_record`'s guard + message widened.
  * **`detail_code` catalogue.** `OfflineSyncResultSerializer.detail_code`
    gains a `help_text` enumerating every value grouped by outcome
    (`payment_mismatch`, `stock_not_available`, `outside_window`,
    `duplicate_sequence`, `revoked`/`replaced`/`force_closed`, …); `outcome`
    is now `ChoiceField(OfflineSyncOutcome.choices)` reusing the existing
    `OutcomeEnum` (pinned via a new `ENUM_NAME_OVERRIDES` entry so
    `--fail-on-warn` stays clean). `docs/FRONTEND_HANDOFF.md` §Offline steps 6
    & 9 gain the result-shape spec, the `detail_code` table with suggested
    wording, and the widened review-queue rule.
  Files: `apps/sales/api/offline_serializers.py`,
  `apps/sales/api/offline_views.py`, `apps/sales/services/offline.py`,
  `config/settings/base.py`, `openapi.yml`, `docs/FRONTEND_HANDOFF.md`,
  `docs/PROGRESS.md`. New: `apps/sales/tests/test_offline_sync_contract.py`
  (14). **No model change, no migration.** Full battery re-run — results in
  the session report. **Not committed, not pushed, not deployed.**

- 2026-08-31: **G23 follow-up — `GET /offline/sales/{client_sale_id}/` carries
  the sync-record resolution state** (strict TDD; failing tests first;
  **nothing committed / pushed / deployed**). *Root cause:* the endpoint's
  200 body was `type: object, additionalProperties: {}` (free-form) and only
  reported the official-Sale mapping (`synced`, `outcome`,
  `official_receipt_number`). A cashier device polling it could detect "became
  a Sale" but not "owner resolved the sync-record **without** a Sale" — the
  normal `REJECTED` path, and possible for `CONFLICT` — so those "needs the
  owner" rows stuck on the device forever. *Fix (record-centric; explicit
  schema):* new `OfflineSaleLookupSerializer` (`apps/sales/api/
  offline_serializers.py`) → `OfflineSaleLookup` component:
  `client_sale_id, device_sequence, outcome (OutcomeEnum), detail_code
  (+ the shared catalogue help_text), sale_id|null, receipt_number|null,
  resolved, resolved_at|null, resolution_note`. `OfflineSaleLookupView` now
  loads the `OfflineSaleSyncRecord` (branch-scoped) and **binds to
  `record.authorization.cashier_id == request.user.id`** — any other user
  (including the branch owner), a `client_sale_id` with no record, or a cross-
  branch id → an indistinguishable `404 not_found`. Returned for a record in
  any outcome, not only once an official Sale exists, so a resolved
  CONFLICT/REJECTED shows `resolved: true` + `resolved_at` + `resolution_note`
  while `outcome`/`sale_id` are unchanged — the device clears the row. Removed
  the now-unused `Sale` import from the view. **Breaking to the (undocumented,
  free-form) old body:** `synced` dropped; `official_receipt_number` →
  `receipt_number`. operationId `offline_sales_retrieve` and the path/method
  are unchanged → contract snapshot unaffected. `docs/FRONTEND_HANDOFF.md`
  §Offline step 7 rewritten with the exact shape + binding; step 8 gains the
  explicit **end-session is owner-only** note (a cashier gets `403
  permission_denied` — existing Stage 16 design, unchanged). Tests updated:
  `test_offline_api.py::test_client_sale_id_maps_to_official_sale_after_sync`,
  `tests/acceptance/test_branch_isolation.py` (`offline-sale-lookup` row now
  points at a real sync record). New: `apps/sales/tests/
  test_offline_sale_lookup.py` (11) — accepted shape, held CONFLICT visible,
  CONFLICT/REJECTED resolved-without-a-Sale no longer stuck, wrong cashier /
  owner / cross-branch / unknown id = 404 (indistinguishable), 401
  unauthenticated, no cost/token/customer leak, OpenAPI schema. `openapi.yml`
  regenerated (`--fail-on-warn` clean, byte-stable). **No model change, no
  migration.** Full battery re-run — results in the session report. **Not
  committed, not pushed, not deployed.**

- 2026-08-31: **Safe offline-sale reconciliation** (approved accounting +
  inventory design; strict TDD; **nothing committed / pushed / deployed**).
  Replaces the unsafe path where an owner could mark a `CONFLICT` / `REJECTED`
  / `OWNER_REVIEW_REQUIRED` sync record `resolved` with only a note while
  stock, revenue, payments, COGS and profit stayed wrong.
  * **Model** (`apps/sales/migrations/0004`, `apps/inventory/migrations/0002`):
    new `OfflineSaleReconciliation` (1:1 with `OfflineSaleSyncRecord`,
    immutable; `kind` = `RECORDED_AS_SALE` | `REFUNDED_AND_RETURNED` |
    `LINKED_EXISTING_SALE`; `resolved_by` / `resolved_at` / `explanation` /
    `sale` / `receipt_number` / `refund_total` / `linked_manually` /
    `completion_time_substituted`), child `OfflineReconciliationRefund`
    (evidence, must total the offline amount) and `OfflineReconciliationCount`
    (physical count per affected variant + `correction_delta`). New
    `MovementType.OFFLINE_RECONCILIATION` (`StockMovement.movement_type`
    widened 12 -> 24).
  * **Service** `apps/sales/services/offline_reconciliation.py::
    reconcile_sync_record` — one `transaction.atomic()`, `select_for_update`
    on the sync record. `RECORDED_AS_SALE`: verifies the retained signed token
    + branch/device/cashier binding (`offline_data_untrusted` otherwise),
    recomputes the total from the frozen snapshot, validates the retained
    payments cover it (owner supplies only the missing Transfer/POS
    references), rebuilds each affected balance to `counted_on_hand +
    qty_in_sale` via an `OFFLINE_RECONCILIATION` correction (never restock,
    cost basis untouched), then calls the authoritative `create_sale`
    (`source=OFFLINE`, `fixed_prices`, original `client_sale_id`, in-window
    `completed_at`, COGS from the locked `average_unit_cost`) and asserts the
    final balance equals the count and is non-negative. `REFUNDED_AND_RETURNED`:
    full return + full refund only; refund evidence must total the offline
    amount; creates **no** Sale / Payment / SaleReturn / stock movement.
    `LINKED_EXISTING_SALE`: fallback — validates a COMPLETED branch sale, not
    already linked, matching total. Idempotent per record (1:1 + row lock +
    `create_sale` key); refund vs sale reconciliations are mutually exclusive
    (`offline_record_already_resolved`).
  * **`resolve_sync_record`** now `409 offline_reconciliation_required` for the
    three accounting outcomes (and its existing `offline_record_not_resolvable`
    otherwise); the `resolve/` endpoint stays as a guard. Historical
    `resolved=True` rows untouched.
  * **API**: `POST /api/v1/offline/sync-records/{id}/reconcile/` (owner + MFA)
    -> `OfflineReconciliationResult`. `OfflineSaleLookup` gains a safe
    `resolution_kind` (nullable enum, no amounts) for the cashier device.
  * **Contract**: `openapi.yml` regenerated (`--fail-on-warn` clean,
    byte-stable; +1 path / +1 operationId / `OfflineReconciliation*` schemas +
    `MovementType.OFFLINE_RECONCILIATION`); `openapi_contract_snapshot.json`,
    `security_classification.py` (`offline-sync-record-reconcile`) and
    `test_branch_isolation.py` updated; `ENUM_NAME_OVERRIDES` pins `KindEnum`
    (catalogue) + `OfflineReconciliationKindEnum`. `docs/FRONTEND_HANDOFF.md`
    gains an **Offline sale reconciliation** section.
  * **Tests**: `apps/sales/tests/test_offline_reconciliation.py` (34) +
    `test_offline_reconciliation_concurrency.py` (3 real-thread PostgreSQL);
    existing `test_offline_api.py` / `test_offline_sale_lookup.py` /
    `test_offline_sync_contract.py` updated for the note-only removal.
  * **Conflicts reported** (brief vs model): payment references / customer
    phone are not retained (owner supplies references; customer is name-only);
    `MovementType` needed a new value + column widen; `outside_window`
    timestamps are not preserved (now-substituted, flagged); note-only
    resolution removed for the three outcomes; a cashier hitting the owner-only
    endpoint gets `403` (established pattern) while cross-branch/bad-id gets
    `404`. No inventory invariant weakened. **No** commit / push / deploy.

- 2026-08-31: **Offline reconciliation — provisional-approval corrections**
  (verification pass on the design above; automated backend tests only;
  **nothing committed / pushed / deployed**; the frontend has NOT been asked to
  sync the contract).
  * **Model** (`apps/sales/migrations/0004` rebuilt in place — still unapplied
    to any real DB): `OfflineSaleReconciliation` gains `amount_source`
    (`SNAPSHOT_VERIFIED` | `OWNER_ATTESTED`, blank for `RECORDED_AS_SALE`) and
    `link_verification` (`FULL` | `PARTIAL` | `MANUAL_ATTESTED`, blank off
    `LINKED_EXISTING_SALE`). New `TextChoices` `OfflineAmountSource` /
    `OfflineLinkVerification`.
  * **P1 — `LINKED_EXISTING_SALE` safety.** The target is now compared
    field-by-field against every trustworthy retained value — item lines +
    quantities (`redacted_payload`), payment methods + amounts
    (`retained_payments`), and the cryptographically **verified** snapshot
    total (falling back to the retained payment sum). Any comparable field that
    mismatches -> `409 sale_incompatible`, so an unrelated same-branch sale can
    no longer be attached just because the branch + total line up. When
    **nothing** is comparable (retained data too damaged) the link needs
    `owner_attestation: true` (owner + MFA is already enforced by the view) + a
    ≥40-char `explanation`; it is stamped `link_verification: MANUAL_ATTESTED`,
    `amount_source: OWNER_ATTESTED`, `linked_manually: true`. `FULL` requires
    all three checks present and matched; else `PARTIAL`. Cross-branch / unknown
    `sale_id` still 404.
  * **P2 — `REFUNDED_AND_RETURNED` untrusted amount.** The "full offline
    amount" is recomputed from the verified frozen snapshot
    (`amount_source: SNAPSHOT_VERIFIED`) — including for a `payment_mismatch`
    REJECTED record (snapshot prices are authoritative, not the device's
    recorded paid figure). If the snapshot cannot be verified (broken
    signature / binding, malformed retained items) the backend refuses to
    compare against an untrusted number: `409 offline_total_unverifiable`
    unless the request carries `owner_attestation: true` +
    `attested_offline_total` + a ≥40-char `explanation`, in which case the
    result is flagged `amount_source: OWNER_ATTESTED` (never presented as
    verified). New codes `offline_total_unverifiable`, `attested_total_required`,
    `attestation_explanation_too_short`.
  * **P3 — payment-reference association.** `payment_references` is now
    **structured** — `[{ payment_index, reference }]` with 0-based
    `payment_index` into the record's `retained_payments` — so a split
    Transfer + POS batch is never mis-mapped by order. New owner-only read
    model `retained_payments` on `GET .../sync-records/{id}/`
    (`[{payment_index, method, amount, reference_required}]`, never a
    reference). New codes `payment_reference_index_invalid`,
    `payment_reference_duplicated`, `payment_reference_unexpected`.
  * **P4 — historical unsafe resolutions.** New management command
    `list_unsafe_offline_resolutions` (+ `unsafe_offline_resolutions()` helper):
    lists accounting-outcome records with `resolved=True` and **no**
    `OfflineSaleReconciliation`; prints only ids / branch code / outcome /
    detail code / timestamps / resolver / whether an official Sale exists —
    no customer or payment data. `--reopen` clears `resolved` (audited via
    `offline.reopen_unsafe_resolution`) so the record re-enters the queue;
    it invents no Sale, refund or stock movement. **Current local dev DB: 5
    such records**, all LEKKI-branch demo/test residue from
    `seed_demo` + earlier note-only test runs — to be `--reopen`ed and
    reconciled (or explained as demo data) before any deploy.
  * **P5 — refund-reference disclosure.** Resolved: owner-entered
    `reconciliation.refunds[].reference` **is** returned on the owner + MFA
    reconcile response (so the owner can confirm what was recorded); it is
    excluded from every `AuditLog` row (`record_audit` `after` carries
    `refund_total` only) and the cashier `OfflineSaleLookup` has no `refunds`
    field at all. The device's *original* offline payment reference was never
    stored. Serializer docstrings + the `reconcile` `@extend_schema`
    description + `docs/FRONTEND_HANDOFF.md` updated to say so.
  * **P6 — repo hygiene.** `.gitignore` already ignores `private-media/`
    (verified: `git check-ignore` matches, `git status` clean of it); the
    user's uploaded files under it are untouched.
  * **Contract**: `openapi.yml` regenerated (`--fail-on-warn` clean,
    byte-stable — identical on a second run). **No new path / operationId**
    (`openapi_contract_snapshot.json` unchanged, contract test green); added
    component schemas `_RetainedPayment`, `_ReconcilePaymentReferenceRequest`,
    `AmountSourceEnum`, `LinkVerificationEnum`, plus the new fields on
    `OfflineReconciliation` / `OfflineSyncRecord` / `OfflineReconcileRequest`.
    No `ENUM_NAME_OVERRIDES` change needed (no collision).
  * **Tests**: `test_offline_reconciliation.py` 34 -> 52 (+P1 link-safety ×8,
    +P2 total-source ×4, +P3 structured references ×3, +P4 command ×1,
    +P5 disclosure ×1, +contract ×1; `test_transfer_payment_...` migrated to
    the structured request). Full battery: **914 passed / 5 skipped** on
    PostgreSQL 18, 94% coverage; ruff / format / `manage.py check` /
    `makemigrations --check` / `check --deploy --fail-level WARNING` (prod) /
    OpenAPI `--fail-on-warn` + byte-stable / secret + action-pin scans /
    pip-audit all clean. **No** commit / push / deploy.

- 2026-08-31: **Offline reconciliation — refund amount = money ACTUALLY
  COLLECTED** (second correction pass; automated backend tests only; **nothing
  committed / pushed / deployed / synced to the frontend**).
  * **Bug**: `REFUNDED_AND_RETURNED` required the refund evidence to total the
    *verified catalogue snapshot* total. For a `payment_mismatch` record
    (snapshot ₦2,000, device took ₦1,999) that forced either an over-refund or
    false refund evidence.
  * **Model** (`apps/sales/migrations/0004` rebuilt in place again — still
    unapplied to any real DB): `OfflineAmountSource` gains
    `RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT` (column widened 20 -> 40);
    `OfflineSaleReconciliation` gains owner-only diagnostics
    `verified_snapshot_total` and `retained_payments_total` (nullable
    Decimals). `SNAPSHOT_VERIFIED` / `OWNER_ATTESTED` / the
    `OfflineLinkVerification` help-text reworded to their exact meanings.
  * **Service** (`_resolve_offline_total` -> `_resolve_collected_amount`, new
    `_retained_payments_total`): the refund figure is the money **actually
    collected**. Trusted automatically **only** when the retained device
    payments parse cleanly **and equal** the verified snapshot total
    (`amount_source: RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`). On any
    disagreement, or unverifiable retained data, the catalogue total is **not**
    used and the device figure is **not** trusted: `409
    offline_total_unverifiable` unless `owner_attestation: true` +
    `attested_offline_total` (amount collected) + ≥40-char `explanation`
    (`OWNER_ATTESTED`); `refunds` must then total the attested amount exactly.
    A non-positive collected amount -> `409 no_payment_to_refund` (not a
    refund; no fake positive evidence accepted). The verified snapshot total
    and retained payment total are recorded as the two owner-only diagnostic
    fields — never in the cashier `OfflineSaleLookup`, never in an
    `AuditLog` row (audit `after` now also carries the categorical
    `amount_source` / `link_verification`, no amounts). Rule kept: no Sale /
    revenue / COGS / receipt / stock movement for a full return + full refund.
  * **Dimensions fix**: `LINKED_EXISTING_SALE` `FULL` is now defined once,
    everywhere, as **three dimensions** — (1) line items & quantities,
    (2) payment methods & amounts, (3) transaction total — all comparable and
    all matched (`checks_possible == 3 and checks_passed == 3`); `PARTIAL` =
    every comparable one matched, fewer than three comparable; `MANUAL_ATTESTED`
    = none comparable. Service comment, model help-text, `@extend_schema`,
    serializer docstring and `FRONTEND_HANDOFF.md` all use this wording.
  * **Contract**: `openapi.yml` regenerated (`--fail-on-warn` clean,
    byte-stable). **No new path / operationId** (contract snapshot unchanged);
    `AmountSourceEnum` gains a value, `OfflineReconciliation` gains
    `verified_snapshot_total` / `retained_payments_total`. New stable code
    `no_payment_to_refund`.
  * **Tests**: `test_offline_reconciliation.py` 52 -> 56 — `TestRefundTotalSource`
    replaced by `TestRefundCollectedAmount` covering: retained==snapshot
    succeeds (`RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`); retained ₦1,999 vs
    ₦2,000 refund-of-catalogue without attestation rejected; owner attests
    ₦1,999 + refunds ₦1,999 succeeds (`OWNER_ATTESTED`, diagnostics
    ₦2,000/₦1,999); attested ≠ refund evidence rejected; broken-signature and
    malformed-items each need attestation; zero collected can't be faked as a
    refund; diagnostics + refund references stay owner-only (cashier + audit
    checked).
  * **Historical demo records**: the **5** unsafe LEKKI-branch note-only
    resolutions remain **listed only** — `list_unsafe_offline_resolutions`
    still reports them; they were **not** mutated. Before any deployment they
    MUST be either `--reopen`ed and reconciled through the endpoint, or the
    demo database safely reseeded (`seed_demo` is idempotent and refuses under
    production settings). They must not be treated as reconciled.
  * **Known limitation (accepted)** — *corrupted zero-payment record*: a
    `REJECTED` / `CONFLICT` / `OWNER_REVIEW_REQUIRED` record whose established
    "amount actually collected" is not positive (retained payments empty /
    unparseable and the owner cannot truthfully attest a positive figure)
    **cannot currently be reconciled**. `RECORDED_AS_SALE` has nothing to
    charge, `REFUNDED_AND_RETURNED` returns `409 no_payment_to_refund` (by
    design — no fake positive refund evidence is accepted), and
    `LINKED_EXISTING_SALE` needs a real matching sale. Such a record therefore
    **remains unresolved in the review queue** and keeps blocking a plain
    `end-session`. Resolving it needs a future explicit no-transaction /
    void resolution type, or controlled administrative handling (DBA-level
    review) — deliberately out of scope here.
  * Full battery re-run — see session report. **No** commit / push / deploy /
    frontend sync.

- 2026-09-05: Tasks 1–6 committed as `2fd1e82` on `main` (follow-up commit,
  no history rewritten, not pushed). Contract confirmed **final** and the
  "do not wire the reconcile screen yet" callout in `FRONTEND_HANDOFF.md`
  replaced with the settled note (+ the `manage.py migrate` server
  prerequisite). Frontend (`viable-stone-frontend-e3`) reported it built the
  full reconcile flow (all 3 kinds, 27 codes, `retained_payments` /
  `resolution_kind`, cashier-safe diagnostics; all their gates green).
  * **Backlog — G24 (deferred, low-risk, defensive).** `OfflineSyncRecord.
    redacted_payload` has **no OpenAPI schema** — it serialises as an opaque
    `readOnly` blob. The frontend's RECORDED_AS_SALE physical-count form needs
    the affected-variant list and currently reads the blob defensively
    against the shape `apps/sales/services/offline.py::_redacted()` /
    `offline_reconciliation.py::_affected_quantities()` produce (returning
    `[]` on anything unexpected). Requested: publish that shape as a typed
    `readOnly` object (named properties: `device_sequence`, `customer_name`,
    `items: [{variant_id, quantity}]`, `payments: [{method, amount,
    has_reference}]`) so the frontend stops reverse-engineering internals.
    Purely additive to the schema; no runtime or contract-surface change.
    **Not implemented this turn** (owner deferred).
  * **Dev-DB migration-state note.** `sales.0004` was rebuilt in place three
    times during Tasks 5–6. A dev/demo instance that applied an *earlier*
    `0004` shows it as `[X]` in `showmigrations` but is missing
    `verified_snapshot_total` / `retained_payments_total` and has
    `amount_source` at `varchar(20)` — every `reconcile` call then `500`s in
    `_make_reconciliation`. Fix on such an instance: `manage.py migrate sales
    0003` then `manage.py migrate`. The committed `0004` is already final;
    fresh installs / CI / the test DB are unaffected.

- 2026-09-06: **Applied the dev-DB `sales.0004` fix + closed the stale-session
  sync gap** (frontend integration follow-ups; not committed).
  * Ran `migrate sales 0003 && migrate` on the dev DB — schema is now correct
    (`amount_source varchar(40)` + the two diagnostic columns). **Data
    mishap:** the reverse dropped the 3 (thought-empty) reconciliation tables
    while one real `OfflineSaleReconciliation` + `OfflineReconciliationRefund`
    row existed (created between the empty-check and the run). Rebuilt that row
    from the surviving `AuditLog` `offline.reconcile` entry + the retained
    request body — `REFUNDED_AND_RETURNED`, `OWNER_ATTESTED`, `refund_total
    222500.00`, one TRANSFER refund `222500.00` ref "sent back", `resolved_at`
    preserved (`created_at` necessarily new; `verified_snapshot_total` /
    `retained_payments_total` unrecoverable → NULL). An
    `offline.reconcile_row_recreated` audit row documents the reconstruction.
    `list_unsafe_offline_resolutions` back to 5.
  * **Stale-session sync gap (CLOSED-token) — fixed.** `POST /offline/sync/`
    verifies token signature + binding but not `authorization.status`, and
    `sync_offline_batch` only diverted to `OWNER_REVIEW_REQUIRED` for
    `needs_owner_review` = {FORCE_CLOSED, REVOKED, REPLACED}. A device holding a
    stale token for a **CLOSED** session (or an **expired** ACTIVE one — window
    lapsed) synced its queue as live ACCEPTED/CONFLICT/REJECTED. Observed in
    the frontend's access log (a sync 11s after `end-session`).
    - `OFFLINE_REVIEW_STATUSES` gains `CLOSED` (now = every non-ACTIVE status).
    - `sync_offline_batch`: `review_mode = needs_owner_review or is_expired`.
    - `_sync_one` review-branch `detail_code`: `closed` / `force_closed` /
      `revoked` / `replaced` for a non-ACTIVE status, `expired` for an
      ACTIVE-but-lapsed window.
    - `OFFLINE_SYNC_DETAIL_CODES_DOC` + `FRONTEND_HANDOFF.md` list the two new
      OWNER_REVIEW_REQUIRED codes (`closed`, `expired`). **No path / method /
      operationId / component-schema change** — only the `detail_code`
      help-text string; `openapi.yml` regenerated (`--fail-on-warn` clean,
      byte-stable), `openapi_contract_snapshot.json` unchanged.
    - `test_offline_sync.py::test_late_sync_of_an_in_window_sale_still_accepted`
      **reversed** → `..._after_the_window_expired_routes_to_owner_review`
      (deliberate: an expired session's in-window late sale now holds for the
      owner instead of auto-accepting). New tests: CLOSED session → all
      OWNER_REVIEW_REQUIRED (`closed`); expired auth → OWNER_REVIEW_REQUIRED
      (`expired`). `test_offline_models.py` helper test extended for all four
      review statuses.
  * **Fixed — calendar-rollover test failures** (pre-existing, unrelated to the
    gap fix; surfaced only because the date advanced past 2026-08-31):
    `test_business_journeys.py::{test_journey_expenses_and_reports,
    test_journey_resellable_and_damaged_returns}` hardcoded
    `period=custom&start=2026-08-01&end=2026-08-31` while creating all activity
    "now" → now query `period=month` with `expense_date = today`;
    `test_report_contract.py::test_limit_caps_rows_and_page_params_are_ignored`
    froze the sales to Aug 10 but not the `period=month` query → the whole test
    body (sales + query) is now inside one `freeze_time` block. Full suite back
    to green.
