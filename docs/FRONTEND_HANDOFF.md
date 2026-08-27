# Frontend Handoff

The single source of truth for request/response shapes is
[`openapi.yml`](../openapi.yml) at the repo root (OpenAPI 3.0, validated in CI
with zero warnings). This document explains the cross-cutting behaviour that a
schema cannot express. **No endpoints are invented here** — everything below is
in `openapi.yml`.

The contract is **frozen**: its path/method/operationId surface is snapshotted
in `tests/acceptance/openapi_contract_snapshot.json` and a test fails the build
if it changes without a reviewed update. 99 paths, 139 operations. Treat a
`code` value or a route change as a breaking-change signal.

## API base URL

* Local: `http://127.0.0.1:8000/api/v1/`
* All application routes are under `/api/v1/`.
* `GET /api/v1/health/live/` — process up. `GET /api/v1/health/ready/` — DB +
  Redis up. Bodies are minimal (`{"status": ...}` plus per-check states).

## Auth: session + CSRF + MFA

Cookie-session auth (no bearer tokens).

1. `POST /api/v1/auth/login/` `{username, password}`.
   * Employee → logged in immediately; response includes the safe profile.
   * Owner / tech-admin → response is `{"mfa_required": true, ...}` and the
     profile is withheld until MFA is cleared.
2. `POST /api/v1/auth/mfa/verify/` `{token}` (6-digit TOTP) — or
   `POST /api/v1/auth/mfa/recovery/` `{code}` with a one-time recovery code.
3. `POST /api/v1/auth/logout/`.
4. `GET /api/v1/auth/me/` — the current safe profile (never returns password
   hashes, TOTP secrets, recovery codes or full phone numbers).

**Cookies:** `vs_sessionid` (HttpOnly) and `vs_csrftoken` (readable by JS).
`SameSite=Lax`; `Secure` in production.

**CSRF:** every unsafe method (`POST/PUT/PATCH/DELETE`) needs the
`X-CSRFToken` header set to the `vs_csrftoken` cookie value.
`GET /api/v1/auth/csrf/` primes the cookie. A missing or stale token → `403`
`{"code": "permission_denied", ...}`; an unauthenticated call → `401`.

**Session expiry:** 12 h idle (configurable). On `401` from any endpoint, send
the user back to login.

## Roles & permissions

| Role | Can |
|---|---|
| `EMPLOYEE` | create sales & returns *requests*, see **own** sales, catalogue/stock reads, offline checkout & sync |
| `OWNER` | everything for **their** branch: approvals, discounts, expenses, reports, offline device/authorization admin, audit log |
| `TECH_ADMIN` | account/branch administration, API docs; **not** a business actor (no sales/reports) |

Every list is **branch-scoped**. Requesting another branch's object by id
returns **`404`** (never `403` — ids are not confirmed to exist).

## Error envelope

Every non-2xx body:

```json
{ "code": "stock_not_available", "message": "Only 3 unit(s) …",
  "field_errors": { "quantity": ["Must be greater than zero."] },
  "request_id": "0e5c…" }
```

* `code` — stable, machine-readable. Branch UI logic on this, not `message`.
* `field_errors` — per-field validation messages ({} when not applicable).
* `request_id` — also returned as the `X-Request-ID` response header; quote it
  in bug reports.
* `429` includes a `Retry-After` header. `409` is a genuine state conflict
  (e.g. `sale_in_progress`, `offline_session_active`) — safe to retry after
  resolving the cause.

## Pagination

List endpoints: `?page=`, `?page_size=` (default 25, max 100).

```json
{ "count": 137, "next": "…?page=3", "previous": "…?page=1", "results": [ … ] }
```

Ordering via `?ordering=field` / `?ordering=-field` where documented; text
search via `?search=` where documented. The `search` value is always treated as
a literal substring — `%`, `_` and SQL fragments match nothing, they do not
error.

## Money & dates

* **Money is always a string.** JSON model/report fields are exact decimal
  strings with 2 places — `"1000.00"`, `"0.00"`, `"-5.00"`. Parse with a
  decimal library, never `parseFloat`. Send money as a string too.
* **Currency has no symbol** in model/report JSON. The **JSON receipt**
  (`GET /sales/{id}/receipt/`) and the PDF are display documents: there the
  money fields are *grouped for printing* — `"1,234.00"` — and currency shows
  as `NGN` (the built-in PDF fonts have no ₦ glyph). Do not parse receipt money
  fields as numbers; show them as-is.
* **Timestamps** (`created_at`, `completed_at`, `issued_at_iso`, …) are ISO-8601
  with a `+01:00` offset (Africa/Lagos). `*_iso` fields are machine-readable;
  `issued_at` on the receipt is a pre-formatted human string.
* **Business dates** (`expense_date`, report `start`/`end`, restock `date`) are
  plain `YYYY-MM-DD` in Africa/Lagos local time. Report ranges are
  **end-inclusive**. Send dates as `YYYY-MM-DD`; `31/02/2026` and other formats
  are rejected `400`.

## Rendering API text safely (XSS)

The API returns user- and owner-entered text (product names, descriptions,
customer names, expense notes, reasons, reviewer notes) **verbatim** as plain
JSON strings. It does **not** HTML-escape them and does not sanitise them —
that is the client's job at render time.

* Render every API string with `textContent` / React `{value}` / Vue `{{ }}` /
  Angular interpolation — anything that auto-escapes.
* **Never** feed an API value to `innerHTML`, `outerHTML`,
  `dangerouslySetInnerHTML`, `v-html`, `[innerHTML]`, `document.write`,
  `insertAdjacentHTML`, or a non-escaping template.
* Do not build DOM from API strings by concatenation. Do not put an API string
  into an `href`/`src` without validating the scheme (`javascript:` etc.).

The backend already guarantees control characters (NUL, C0/C1) are rejected on
input and that text reaching the PDF is escaped, so a value like
`<script>alert(1)</script>` is stored and returned as literal text and is inert
unless *you* inject it as HTML.

## Fields the frontend must never compute or trust

The server is the sole authority for these. Always read them from the response;
never calculate them client-side and never send them expecting them to be used:

| Field(s) | Owned by |
|---|---|
| `subtotal`, `discount_total`, `total`, `line_total`, `change_due` | `create_sale` / `finalise_draft` from active prices |
| `unit_price_snapshot`, `product_name_snapshot`, `sku_snapshot`, `variant_description_snapshot` | captured at sale time; immutable |
| `unit_cost_snapshot`, `average_unit_cost`, `stock_value`, `cogs`, `gross_profit`, `net_profit` | owner-only; never in employee responses |
| `receipt_number` | per-branch-per-day sequence, assigned on completion |
| `status`, `status_label` | server state machine |
| approved discount `amount` | owner approval only (`0 < amount < subtotal`) |
| refund amounts on a return | server, net of any discount |
| offline sync `outcome`, `receipt_number`, recomputed prices/totals | server recomputes everything from the **signed snapshot**; client prices/totals are ignored |
| any `id`, `created_at`, `updated_at`, `branch` | server-assigned; sending them is silently ignored |

Sending an unknown or read-only field is **not an error** — it is silently
dropped. Rely on that: a newer client can send an extra field to an older
backend without breaking.

## Idempotency keys

Client-generated UUIDs make retries safe:

* **Sales:** `client_sale_id` — unique per `(branch, client_sale_id)`. Re-POST
  with the same id returns the same completed sale (`200`), not a duplicate.
* **Returns:** `client_return_id`.
* **Protected stock adjustments:** `client_adjustment_id`.
* **Offline sync:** each offline sale carries `client_sale_id` + a strictly
  increasing `device_sequence`; a batch may be re-sent verbatim.

Generate the id **before** the first attempt and reuse it for every retry of
that logical action.

## Receipts / PDF

* `GET /api/v1/sales/{id}/receipt/` — JSON receipt from immutable snapshots.
* `GET /api/v1/sales/{id}/receipt.pdf` — `Content-Type: application/pdf`,
  `Cache-Control: private, no-store`. A4, from the same snapshots; contains **no**
  cost, profit, internal id or audit data. Same role + branch scoping as the
  sale (cross-branch → `404`).
* Currency renders as `NGN 1,234.00` (the built-in PDF fonts have no ₦ glyph).
* Discounted sales show `Subtotal / Owner-approved discount / Final total`.

## Notifications & Web Push

* `GET /api/v1/notifications/` (own only), `GET …/unread-count/`,
  `POST …/{id}/read/`, `POST …/read-all/` (both idempotent). Clients cannot
  create/edit/delete notifications.
* Types: `LOW_STOCK`, `OUT_OF_STOCK`, `APPROVAL_REQUESTED`, `APPROVAL_APPROVED`,
  `APPROVAL_REJECTED`. Each has a safe `title`/`message`, an allow-listed
  `action_path`, and `related_object_type`/`related_object_id`.
* Web Push: `GET /api/v1/push-subscriptions/public-key/` → the browser-safe
  VAPID public key. `POST /api/v1/push-subscriptions/` upserts a browser
  subscription `{endpoint, p256dh, auth, user_agent?}` (HTTPS endpoint required
  unless `localhost`). `POST …/{id}/deactivate/`, `DELETE …/{id}/`. Push is
  best-effort; the in-app list is the record of truth.

## Offline authorization & synchronization contract

Full rules in `openapi.yml` under the `Offline` tag; the shape:

1. **Owner (MFA)** registers the till: `POST /api/v1/offline/devices/`
   `{name}` (one ACTIVE device per branch).
2. **Owner (MFA)** opens a session: `POST /api/v1/offline/authorizations/`
   `{device, cashier}` → returns `signed_token` and the fixed **catalogue
   snapshot** (active variants: name, SKU, fixed price, quantity, low-stock
   level — nothing else). Valid ≤ 24 h.
3. While a session is ACTIVE the branch's online stock-changing endpoints
   return `409 offline_session_active`.
4. The device sells offline against the snapshot, decrementing **local** stock,
   printing a temporary receipt labelled
   `OFFLINE RECEIPT — PENDING SYNCHRONIZATION` (no official number). Optional
   preview: `POST /api/v1/offline/temporary-receipt/`.
5. When online: `POST /api/v1/offline/sync/`
   `{authorization_token, sales: [{client_sale_id, device_sequence,
   offline_created_at, items, payments, customer_name?, customer_phone?}]}`.
   The server recomputes every price/total from the signed snapshot and returns
   one result per sale: `ACCEPTED` (+ `sale_id`, `receipt_number`),
   `DUPLICATE`, `CONFLICT`, `REJECTED`, `OWNER_REVIEW_REQUIRED`. Retrying the
   batch is safe.
6. `GET /api/v1/offline/sales/{client_sale_id}/` maps a client id to the
   official Sale + receipt number once synced.
7. `POST /api/v1/offline/authorizations/{id}/end-session/`
   `{force?, mfa_confirmed?, reason?}` — a plain end needs zero unresolved
   records; a force-end (with pending sales) needs `mfa_confirmed: true`.
8. Owner reviews `GET /api/v1/offline/sync-records/?outcome=CONFLICT` and
   `POST …/{id}/resolve/` `{note}`.
9. Client submits `device_sequence` strictly increasing; **never** submit a
   client-computed price or total — they are ignored.

## Development CORS setup

`config/settings/development.py` allows `http://localhost:3000` and
`http://127.0.0.1:3000` with credentials. To use another dev origin:

```bash
CORS_ALLOWED_ORIGINS=http://localhost:5173 CSRF_TRUSTED_ORIGINS=http://localhost:5173 \
  .venv/Scripts/python manage.py runserver
```

Credentialed CORS requires **exact** origins — no wildcards. The SPA must send
`credentials: 'include'` and the `X-CSRFToken` header on writes.
