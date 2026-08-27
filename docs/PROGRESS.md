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
| 14 | Discounts, approvals, rare returns, refunds, protected adjustments | ⏳ | `reports.profit_report` has a comment marking where approved-return netting plugs in. |
| 15 | Notifications | ⏳ | |
| 16 | One-device offline fixed-price checkout + idempotent sync | ⏳ | |
| 17 | Security hardening, CI, monitoring, deployment, backup/restore docs | ⏳ | Redis required. |
| 18 | Acceptance tests + final OpenAPI contract | ⏳ | |

## Git checkpoints

- `985233f` — **Stages 1–10** (config, accounts+MFA, catalogue, inventory). 127 files.
- `7a04860` — docs: 112-test collection + estimate note.
- `27d9d2e` — **Stage 11** (fully-paid sales, split payments, receipt numbering).
- `29ef932` — **Stage 12** (A4 paid-receipt PDF + JSON receipt).
- Stage 13 — uncommitted at time of writing.
- Branch `setup/backend-foundation`. `.env` excluded (git-ignored); `openapi.yml`
  and `docs/PROGRESS.md` committed in each.

## Test suite — 2026-08-27 (through Stage 13)

```
pytest --collect-only -q  -> 193 tests collected
pytest --create-db        -> 193 passed, 0 failed, 0 skipped   (PostgreSQL 18)
coverage                  -> 92% lines
ruff check .              -> All checks passed
ruff format --check .     -> clean
manage.py check           -> 0 issues
makemigrations --check    -> No changes detected
spectacular --validate    -> exit 0, 0 warnings, 0 errors (openapi.yml)
check --deploy (prod)     -> 0 issues
pip-audit                 -> 7 findings, all in `pip` itself (25.2; upgrade pip).
                             Project runtime deps are clean.
```

### Collected test inventory (193, through Stage 13)

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
| sales/tests/test_create_sale.py | 19 |
| sales/tests/test_sales_api.py | 16 |
| sales/tests/test_receipts.py | 20 |
| sales/tests/test_concurrency.py | 2 |
| finance/tests/test_expenses.py | 14 |
| finance/tests/test_reports.py | 10 |
| **Total** | **193** |

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
