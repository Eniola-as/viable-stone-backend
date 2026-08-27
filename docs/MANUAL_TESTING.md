# Manual Testing Guide

A complete, beginner-friendly walk-through of the whole business. Assumes the
backend runs locally and you drive it with `curl`, HTTPie, the Swagger UI at
`/api/v1/docs/` (dev only), or the future frontend.

## 0. Start the stack

```bash
docker compose up -d                       # local Redis on 127.0.0.1:6379
# PostgreSQL 18 must already be running on the host
.venv/Scripts/python manage.py migrate
DEMO_OWNER_PASSWORD=demo-owner-pw-1 DEMO_EMPLOYEE_PASSWORD=demo-cashier-pw-1 \
  .venv/Scripts/python manage.py seed_demo
.venv/Scripts/python manage.py runserver
```

`seed_demo` creates branch `DEMO`, users `demo-owner` (owner, MFA) and
`demo-cashier` (employee), one product with stock, one sale and one expense.
Re-run any time; `--reset` wipes the demo branch first. It refuses to run under
production settings.

## 1. Sign in

* **Employee:** `POST /api/v1/auth/login/` `{ "username": "demo-cashier",
  "password": "demo-cashier-pw-1" }` → logged in, profile returned.
* **Owner:** same call for `demo-owner` → `{"mfa_required": true}`. Complete
  `POST /api/v1/auth/mfa/setup/` then `/mfa/setup/confirm/` with a TOTP code
  (use any TOTP app on the `otpauth://` URI), then `/mfa/verify/` on later
  logins. Keep the recovery codes shown once.
* Before any write: `GET /api/v1/auth/csrf/`, then send `X-CSRFToken` on every
  `POST/PUT/PATCH/DELETE`.

## 2. Catalogue, inventory & restock (owner)

1. `POST /api/v1/categories/`, `/brands/`, `/products/`, `/variants/`.
2. `POST /api/v1/variants/{id}/price/` — set the active selling price.
3. `POST /api/v1/suppliers/`.
4. Opening stock: `POST /api/v1/inventory/opening-stock/` (once per variant).
5. Restock: `POST /api/v1/restocks/` (draft) → add items →
   `POST /api/v1/restocks/{id}/confirm/`. Confirm updates the weighted-average
   cost and appends immutable `RESTOCK` movements.
6. `GET /api/v1/inventory/` shows balances; `GET /api/v1/inventory/low-stock/`
   the ones at/under their threshold.

## 3. Sales, split payments & receipts (employee or owner)

1. `POST /api/v1/sales/` with `client_sale_id` (a fresh UUID), `items`
   (`variant`, `quantity`), and `payments`. Try a split:
   `[{"method":"CASH","amount":"5000.00","tendered_amount":"6000.00"},
   {"method":"POS","amount":"3000.00","reference":"POS-12"}]`. `change_due`
   comes back `1000.00`.
2. Re-POST the **same** `client_sale_id` → same sale, `200`, no duplicate.
3. `GET /api/v1/sales/{id}/receipt/` (JSON) and
   `GET /api/v1/sales/{id}/receipt.pdf` (A4 PDF, `Content-Type: application/pdf`).
   Confirm no cost/profit/internal-id anywhere.
4. As `demo-cashier`, `GET /api/v1/sales/` shows only **your** sales.

## 4. Discounts & approval (owner-approved)

1. Cashier: `POST /api/v1/sales/drafts/` (internal DRAFT — no payment, no
   stock movement, not a quotation).
2. Cashier: `POST /api/v1/sales/{id}/discount-requests/`
   `{ "amount": "1000.00", "reason": "bulk buyer" }` → status `PENDING_APPROVAL`;
   the branch owner gets an `APPROVAL_REQUESTED` notification.
3. Owner: `POST /api/v1/approvals/{id}/approve/` `{ "amount": "1000.00" }`.
4. Cashier: `POST /api/v1/sales/{id}/finalise/` with payments summing to
   `subtotal − 1000.00`. Editing the cart between request and finalise
   invalidates the approval (`approval_stale`).
5. Reject path: `POST /api/v1/approvals/{id}/reject/` — draft returns to
   `DRAFT`, no side effects.

## 5. Returns & refunds (owner-approved)

1. Employee/owner: `POST /api/v1/sales/{id}/return-requests/`
   `{ reason, lines: [{sale_item, quantity}], client_return_id }`.
2. Owner: `POST /api/v1/approvals/{id}/approve/` with `lines`
   (`condition: RESELLABLE | DAMAGED_OR_OPENED`) and `refunds` summing exactly
   to the return total. RESELLABLE restores stock (`RETURN` movement) and
   reverses COGS; DAMAGED does not.
3. Partial returns never over-refund; a full return equals the amount paid.
4. `POST /api/v1/approvals/{id}/reject/` — nothing changes.

## 6. Expenses & reports (owner)

1. `POST /api/v1/expense-categories/`, then `POST /api/v1/expenses/`
   (`amount > 0`, optional `receipt_file` — must be a real JPEG/PNG/WebP/PDF;
   a renamed script is rejected).
2. `POST /api/v1/expenses/{id}/void/` `{reason}` — once only, amount preserved.
3. `GET /api/v1/reports/profit/?period=month`,
   `/reports/best-sellers/`, `/reports/slow-movers/`, `/reports/inventory/`.
   Approved returns are netted out of revenue and COGS.

## 7. Notifications

* Sell a variant down to its low-stock level → the branch owner gets one
  `LOW_STOCK` notification (selling straight to 0 gives only `OUT_OF_STOCK`).
* `GET /api/v1/notifications/`, `GET …/unread-count/`,
  `POST …/{id}/read/`, `POST …/read-all/` (both idempotent).
* Web Push: `GET /api/v1/push-subscriptions/public-key/`, then
  `POST /api/v1/push-subscriptions/` from the browser.

## 8. Offline checkout & synchronization

1. Owner (MFA): `POST /api/v1/offline/devices/` `{name:"Till 1"}`.
2. Owner (MFA): `POST /api/v1/offline/authorizations/` `{device, cashier}` →
   note `signed_token`. Now `POST /api/v1/sales/` (online) for that branch
   returns `409 offline_session_active`.
3. Cashier: `POST /api/v1/offline/temporary-receipt/`
   `{authorization_token, sale}` → labelled `OFFLINE RECEIPT — PENDING
   SYNCHRONIZATION`, no number.
4. Cashier: `POST /api/v1/offline/sync/` `{authorization_token, sales:[…]}` —
   each sale returns `ACCEPTED` / `DUPLICATE` / `CONFLICT` / `REJECTED` /
   `OWNER_REVIEW_REQUIRED`. Re-send the same batch → all `DUPLICATE`, same
   receipts.
5. Tamper with `authorization_token` → `400 invalid_signature`. Submit as a
   different cashier → `404`.
6. `GET /api/v1/offline/sales/{client_sale_id}/` → official Sale + receipt.
7. Owner: `POST /api/v1/offline/authorizations/{id}/end-session/`
   (`force:true, mfa_confirmed:true` if there are unresolved records). Online
   sales resume.

## 9. Permission & branch-isolation checks

* Employee calling an owner-only endpoint (`/expenses/`, `/reports/…`,
  `/offline/authorizations/`) → `403`.
* Any user requesting another branch's object by id → `404` (not `403`).
* Unauthenticated request → `401`; missing CSRF on a write → `403`.
* Owner without a verified MFA session on a business endpoint → `403`.

## 10. Backup/restore check

```bash
SOURCE_DB=viable_stone bash scripts/backup_restore_drill.sh
```

Compare the "source" and "restored" metric blocks — branch/user/sale/payment
counts and the revenue / payments / expenses totals must be identical. See
`docs/BACKUP_RESTORE.md`.
