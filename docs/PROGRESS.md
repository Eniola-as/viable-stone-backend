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
| 12 | Receipt numbering + printable/PDF receipts | 🔄 | Receipt numbering done in Stage 11; HTML/PDF rendering pending. |
| 13 | Expenses + profit reports | ⏳ | |
| 14 | Discounts, approvals, rare returns, refunds, protected adjustments | ⏳ | |
| 15 | Notifications | ⏳ | |
| 16 | One-device offline fixed-price checkout + idempotent sync | ⏳ | |
| 17 | Security hardening, CI, monitoring, deployment, backup/restore docs | ⏳ | Redis required. |
| 18 | Acceptance tests + final OpenAPI contract | ⏳ | |

## Git checkpoints

- `985233f` — **Stages 1–10** (config, accounts+MFA, catalogue, inventory).
  Branch `setup/backend-foundation`. 127 files. `.env` excluded (git-ignored);
  `openapi.yml` and `docs/PROGRESS.md` committed.

## Test suite — 2026-08-27 (Stages 1–10, CREATEDB granted)

```
pytest --collect-only -q  -> 112 tests collected
pytest --create-db        -> 112 passed, 0 failed, 0 skipped   (PostgreSQL 18)
coverage                  -> 92% lines
ruff check .              -> All checks passed
ruff format --check .     -> clean
manage.py check           -> 0 issues
makemigrations --check    -> No changes detected
spectacular --validate    -> exit 0, 0 warnings, 0 errors (openapi.yml, ~103 KB)
check --deploy (prod)     -> 0 issues
```

### Collected test inventory (112)

| File | Tests |
|---|---|
| accounts/tests/test_auth_api.py | 15 |
| accounts/tests/test_models.py | 22 |
| accounts/tests/test_users_api.py | 11 |
| catalog/tests/test_api.py | 12 |
| catalog/tests/test_models.py | 11 |
| catalog/tests/test_pricing.py | 5 |
| inventory/tests/test_api.py | 13 |
| inventory/tests/test_concurrency.py | 2 |
| inventory/tests/test_opening_stock.py | 4 |
| inventory/tests/test_restock.py | 5 |
| inventory/tests/test_stock_count.py | 5 |
| inventory/tests/test_weighted_cost.py | 7 |
| **Total** | **112** |

### Why the earlier estimate said ~124

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
