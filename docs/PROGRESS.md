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
| 17 | Security hardening, CI, monitoring, deployment, backup/restore docs | ⏳ | Redis required. |
| 18 | Acceptance tests + final OpenAPI contract | ⏳ | |

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
- **Stage 16** — one-device offline fixed-price checkout + idempotent sync
  (signed catalogue snapshot, exclusive session, device-bound batch sync with
  per-sale outcomes).
- Branch `setup/backend-foundation`. `.env` excluded (git-ignored); `openapi.yml`
  and `docs/PROGRESS.md` committed in each.

## Test suite — 2026-08-27 (through Stage 16)

```
pytest --collect-only -q  -> 431 tests collected
pytest --create-db        -> 431 passed, 0 failed, 0 skipped   (PostgreSQL 18)
coverage                  -> 93% lines
ruff check .              -> All checks passed
ruff format --check .     -> clean
manage.py check           -> 0 issues
makemigrations --check    -> No changes detected
spectacular --validate --fail-on-warn -> exit 0, 0 warnings, 0 errors (openapi.yml)
check --deploy (prod)     -> 0 issues
```

### Collected test inventory (431, through Stage 16)

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
| **Total** | **431** |

Stage 14 is complete: 14A (returns / refunds / protected adjustments) +
14B (owner-approved fixed-Naira discount on an internal DRAFT sale).
Stage 15 is complete: durable in-app notifications + best-effort Web Push,
central low/out-of-stock transition detection, approval-workflow alerts.
Stage 16 is complete: one authorised offline device per branch, a versioned
signed catalogue snapshot, an exclusive offline session, and a device-bound
idempotent batch sync that recomputes every price from the snapshot and
returns one outcome per sale.

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
