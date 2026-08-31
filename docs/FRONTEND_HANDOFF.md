# Frontend Handoff

The single source of truth for request/response shapes is
[`openapi.yml`](../openapi.yml) at the repo root (OpenAPI 3.0, validated in CI
with zero warnings). This document explains the cross-cutting behaviour that a
schema cannot express. **No endpoints are invented here** — everything below is
in `openapi.yml`.

The contract is **frozen**: its path/method/operationId surface is snapshotted
in `tests/acceptance/openapi_contract_snapshot.json` and a test fails the build
if it changes without a reviewed update. 101 paths, 142 operations. Treat a
`code` value or a route change as a breaking-change signal.

> **Additive correction (post-Stage 18):** `GET`/`DELETE
> /api/v1/products/{id}/image/` and the additive `image_url` field on product
> read responses. See **Product images** below. No existing field or route
> changed.

> **Additive correction (G2, 2026-08-30):** new
> `GET /api/v1/offline/authorizations/{id}/snapshot/` — the disconnected offline
> device reads the signed fixed-price catalogue snapshot for its active session.
> See **Offline authorization & synchronization contract** step 3. No existing
> field, route or `code` changed.

> **Additive contract corrections (contract-gap audit, 2026-08-30).** No paths,
> methods or operationIds changed; every item below is an added field, an added
> validated query param, or an honest schema fix for a response that was
> already being returned:
> - **G22** — `SaleRead.discount_request`: a nullable, read-only
>   `SaleDiscountRequestSummary` (see **Discount approval state on a sale**).
> - **G21** — `GET /approvals/` gains validated `?status` / `?request_type`.
> - **G3** — `GET /offline/sync-records/` gains validated `?outcome` /
>   `?resolved` (previously applied ad-hoc, now documented + validated).
> - **G18 / G13** — `GET /reports/best-sellers/` & `/reports/slow-movers/` are
>   now correctly typed as **bare arrays** (they never paginated); `?limit`
>   (1..100, default 10 / 20) is the only row cap — `page` / `page_size` do
>   nothing on these two.
> - **G4 / G5** — `GET /inventory/movements/` now documents its required
>   `?variant=` param and its **paginated `StockMovement`** response;
>   `GET /inventory/low-stock/` is a **paginated list**;
>   `GET /inventory/stock-value/` is a `{ "stock_value": "<decimal>" }`
>   summary. Runtime was already like this.
> - **G9** — `DraftCreateRequest.customer` is now the typed
>   `CustomerInlineRequest` (`{name?, phone?}`), same as `SaleCreateRequest`.
> - **G1** — the error envelope is a published `Error` schema +
>   `components.responses.Error`, attached as the `default` response of every
>   operation. See **Error envelope** for the stable `code` catalogue.
> - **G23** — `POST /offline/sync/` now correctly typed: it returns
>   `{ "results": [OfflineSyncResult, …] }` (one object, one entry per sale),
>   **never a bare array** — even for a one-sale batch. `OfflineSyncResult.
>   outcome` is a documented enum and `detail_code` now lists every value.
>   Runtime unchanged.
> - **G23 follow-up** — `GET /offline/sales/{client_sale_id}/` was
>   `additionalProperties: {}`; now a real `OfflineSaleLookup` schema carrying
>   `outcome` / `detail_code` / `resolved` / `resolved_at` / `resolution_note`
>   alongside `sale_id` / `receipt_number`, so the device can clear a held row
>   that the owner resolved **without** creating a Sale. **Binding tightened**
>   to the authorization's cashier (was any branch user); wrong cashier /
>   owner / branch / unknown id → `404`. `synced` and `official_receipt_number`
>   are gone (renamed to the fields above).
> - **Offline review queue** — `pending_review_count`, the plain
>   `end-session` block, and `sync-records/{id}/resolve/` now also cover
>   unresolved **`REJECTED`** records (a rejected offline sale is real money
>   taken on the device; the owner must reconcile it).
> - **end-session is owner-only** (documented, unchanged) — a cashier calling
>   `POST /offline/authorizations/{id}/end-session/` gets
>   `403 permission_denied`.
> - **Safe offline reconciliation (2026-08-31).** New
>   `POST /api/v1/offline/sync-records/{id}/reconcile/` (owner + MFA,
>   polymorphic on `kind`) replaces the unsafe note-only `resolve/`, which now
>   `409`s for accounting-impacting records. The backend creates / links the
>   official Sale itself from the retained payload + verified signed snapshot,
>   or records a full refund + return. `OfflineSaleLookup` gains a safe
>   `resolution_kind`. Adds `MovementType.OFFLINE_RECONCILIATION`. See
>   **Offline sale reconciliation**. **Contract not final — do not wire the
>   reconcile screen yet.** Corrections in review: `payment_references` is now
>   `[{ payment_index, reference }]` (was a positional string list); a
>   `retained_payments` read model on the sync record tells the owner which
>   index is which; `REFUNDED_AND_RETURNED` refunds the money **actually
>   collected** (not the catalogue total) — trusted only when retained payments
>   equal the verified snapshot (`amount_source:
>   RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`), else `owner_attestation` +
>   `attested_offline_total` → `OWNER_ATTESTED` (with owner-only
>   `verified_snapshot_total` / `retained_payments_total` diagnostics); a
>   non-positive collected amount → `no_payment_to_refund`;
>   `LINKED_EXISTING_SALE` gains three-dimension checks + `link_verification`
>   (`FULL` | `PARTIAL` | `MANUAL_ATTESTED`).

## API base URL

* Local: `http://127.0.0.1:8000/api/v1/`
* All application routes are under `/api/v1/`.
* `GET /api/v1/health/live/` — process up. `GET /api/v1/health/ready/` — DB +
  Redis up. Bodies are minimal (`{"status": ...}` plus per-check states).

## Auth: session + CSRF + MFA

Cookie-session auth (no bearer tokens).

1. `POST /api/v1/auth/login/` `{username, password}`.
   * Employee → logged in immediately; `200` body has `user` populated,
     `mfa_required: false`, `mfa_verified: true`.
   * Owner / tech-admin → `200` body is `{"mfa_required": true,
     "mfa_verified": false, "user": null, ...}` — **the sign-in is not
     complete.** Gate the app on `mfa_verified === true` (equivalently
     `user !== null`), **never on the `200` alone.**
   * Wrong credentials → `401` `invalid_credentials` (or `403`
     `account_disabled`); a malformed body → `400`.
   * A `POST /api/v1/auth/login/` attempt **always starts a fresh session** —
     a *failed* attempt also clears any session the browser was already
     carrying, so a rejected sign-in can never leave a previous user logged in.
     Treat any non-2xx from login as "signed out".
2. `POST /api/v1/auth/mfa/verify/` `{token}` (6-digit TOTP) — or
   `POST /api/v1/auth/mfa/recovery/` `{code}` with a one-time recovery code.
   A wrong code → `400` (`mfa_invalid_token` / `recovery_code_invalid`) and the
   session stays unverified — keep the user on the MFA screen; do **not**
   proceed on a `400`.
3. `POST /api/v1/auth/logout/`.
4. `GET /api/v1/auth/me/` — the current safe profile (never returns password
   hashes, TOTP secrets, recovery codes or full phone numbers). `401` = not
   signed in, `403` = signed in but MFA not cleared (or wrong role).

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

**G1 — the envelope is now a published schema.** `openapi.yml` carries an
`Error` component + `components.responses.Error`, and every operation lists it
as its `default` response. Internal exception names, stack traces and secrets
are never in the body.

**Cross-cutting `code` values frontend routing should recognise** (status →
`code`):

| `code` | HTTP | Meaning / action |
|---|---|---|
| `not_authenticated` / `authentication_failed` | 401 | No / invalid session → go to login |
| `permission_denied` | 403 | Role or MFA denial, or a stale CSRF token. Re-prime `GET /auth/csrf/` and retry once; if still 403, drive MFA from the `LoginResponse` / MFA-verify flags (`mfa_required`, `mfa_enrolled`, `mfa_verified`), else show permission-denied. (G7 — no distinct MFA code; the flags are the source of truth.) |
| `csrf_failed` | 403 | Explicit CSRF failure on a write → re-prime CSRF, retry |
| `not_found` | 404 | Missing **or** cross-branch / not-yours — deliberately indistinguishable |
| `validation_error` | 400 | Body/params invalid; see `field_errors` |
| `control_characters` | 400 | A string field contained C0/C1 control chars |
| `throttled` | 429 | Back off using `Retry-After` |
| `method_not_allowed` | 405 · | `parse_error` 400 · `unsupported_media_type` 415 |
| `server_error` | 500 | Generic; quote `request_id` |
| `conflict` (+ many domain codes: `sale_in_progress`, `offline_session_active`, `offline_session_not_active`, `draft_changed`, `approval_stale`, `restock_immutable`, `stock_count_not_draft`, …) | 409 | State conflict — resolve the cause, then retry |

Domain `code` strings (e.g. `stock_not_available`, `invalid_discount`,
`payment_mismatch`, `offline_authorization_expired`, `reason_required`,
`discount_request_pending`) are stable; treat an unknown `code` as "use the
`message` + status-code default". The full set lives in the service/exception
source; the ones above are the ones worth branching on.

## Pagination

List endpoints: `?page=`, `?page_size=` (default 25, max 100).

```json
{ "count": 137, "next": "…?page=3", "previous": "…?page=1", "results": [ … ] }
```

Ordering via `?ordering=field` / `?ordering=-field` where documented; text
search via `?search=` where documented. The `search` value is always treated as
a literal substring — `%`, `_` and SQL fragments match nothing, they do not
error.

## Product list — `GET /api/v1/products/`

Server-side filters, all optional, **AND-combined**, and composable with
`search`, `ordering` (`name` \| `created_at`), `page` and `page_size`. The
result set is always scoped to the caller's branch; the response shape is the
standard paginated `ProductRead` list (unchanged).

| Param | Type | Values | Notes |
|---|---|---|---|
| `category` | string (uuid) | an existing category id | exact match on `category` |
| `brand` | string (uuid) | an existing brand id | exact match on `brand` |
| `kind` | string (enum) | `PAINT` \| `EQUIPMENT` | exact, case-sensitive; this is the model/API field name |
| `is_active` | boolean | `true` \| `false` | **omit** for "any". Employees only ever see active products, so `is_active=false` returns an empty page for them — it never reveals an inactive product |

* An unknown-but-well-formed id (or another branch's id) → `200` with an empty
  page, never a cross-branch leak.
* A malformed value (`category`/`brand` not a uuid, `kind` not in the enum,
  `is_active` not a boolean, empty `?is_active=`) → `400` with the standard
  envelope; every bad param is listed in `field_errors`. Filters are never
  silently ignored.
* No cost / owner-only field is ever added to the rows.

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
| restock `line_total`, `total_cost` | server-calculated purchase total (`Σ quantity × unit_cost`, exact Decimal); already correct on a **DRAFT**, unchanged by confirmation |
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
* **Draft finalise:** optional `client_finalize_id` on
  `POST /sales/{id}/finalise/` — generate one before the first finalise attempt
  and reuse it on every retry (G10). Finalise is also idempotent on its own: a
  second call on an already-`COMPLETED` draft returns that sale unchanged.
* **Protected stock adjustments:** `client_adjustment_id`.
* **Offline sync:** each offline sale carries `client_sale_id` + a strictly
  increasing `device_sequence`; a batch may be re-sent verbatim.

Generate the id **before** the first attempt and reuse it for every retry of
that logical action.

## Product images

Product images are **private**. They are never served from a static/media URL —
the only way to load the bytes is the authenticated, branch-scoped endpoint
below. The bytes stream straight from the storage backend (local disk now, a
private object store later); no filesystem path or storage key is ever exposed.

* **Upload / replace** — `POST /api/v1/products/` or
  `PATCH /api/v1/products/{id}/` with **`multipart/form-data`** and the binary
  field **`image`** (owner only). Unchanged from before.
  * Accepted: **JPEG, PNG, WebP only.** Max **5 MB**.
  * The real file signature is checked — not the filename or the `Content-Type`
    you send. A PDF (or anything else) renamed `photo.jpg`, or a real JPEG sent
    as `shot.png`, is rejected `400` with `field_errors.image`.
* **Read** — product read responses now include an additive, stable field
  **`image_url`**: an absolute URL to
  `GET /api/v1/products/{id}/image/`, or `null` when the product has no image.
  Load the picture from `image_url`. (The older `image` field is retained for
  backwards compatibility and still holds the raw storage reference; do not
  fetch it directly.)
* **`GET /api/v1/products/{id}/image/`**
  * Any authenticated user of the product's branch (owner **or** employee).
  * Returns the image with the correct `Content-Type`
    (`image/jpeg` / `image/png` / `image/webp`),
    `X-Content-Type-Options: nosniff`, and
    `Cache-Control: private, max-age=300`.
  * `401` unauthenticated · `404` for a cross-branch id, an unknown id, **or a
    product that has no image** · (owner-retired `is_active=false` products stay
    visible to the owner, hidden from employees, same as elsewhere).
* **`DELETE /api/v1/products/{id}/image/`** — owner only. Clears the image and
  removes the stored file. `204` on success; `403` for an employee; `404` when
  there is no image to clear. Product creation/replacement still uses the
  `multipart` `image` field above.

## Approvals — polymorphic on `request_type`

`ApprovalRequest` covers two workflows today: **owner-approved discounts** and
**rare returns**. List and detail (`GET /api/v1/approvals/`,
`GET /api/v1/approvals/{id}/`) always expose a stable, read-only
**`request_type`** (`DISCOUNT` | `RETURN`) and **`status`**
(`PENDING` | `APPROVED` | `REJECTED`) — **dispatch on `request_type`**. Owner
only; MFA required; other branches' ids return `404`.

### `POST /api/v1/approvals/{id}/approve/` — body and response depend on `request_type`

| `request_type` | Request body | `200` response |
|---|---|---|
| `DISCOUNT` | `{ "amount": "<decimal string, 0 < amount < subtotal>", "reviewer_note"?: "<string>" }` | the updated **`ApprovalRequest`** (`status: "APPROVED"`, has `request_type`) |
| `RETURN` | `{ "lines": [ { "sale_item": "<uuid>", "quantity": <int ≥ 1>, "condition": "RESELLABLE" \| "DAMAGED_OR_OPENED" } ], "refunds": [ { "method": "CASH" \| "TRANSFER" \| "POS", "amount": "<decimal string>", "reference"?: "<string>" } ], "reviewer_note"?: "<string>", "client_return_id"?: "<uuid>" }` — `lines` and `refunds` are each **required and non-empty**; refund amounts must sum to the server-computed return total (net of any discount) | the created **`SaleReturn`** (`items`, `refunds`, `total`, `client_return_id`; **no** `request_type`) |

In `openapi.yml` the request is `ApproveRequestRequest` (`oneOf`
`ApproveReturnRequest` / `ApproveDiscountRequest`) and the `200` is
`ApproveResult` (`oneOf` `SaleReturnRead` / `ApprovalRequest`).

Sending the wrong body is a **`400`** that names the missing field(s):
`field_errors.amount` (discount body missing / return body sent to a discount),
or `field_errors.lines` + `field_errors.refunds` (return body missing / discount
body sent to a return). Business failures keep their `code`
(e.g. `refund_mismatch`, `approval_not_pending`, `draft_changed`).

### `POST /api/v1/approvals/{id}/reject/` — same for both types

Body `{ "reviewer_note"?: "<string>" }`. `200` response is always the updated
**`ApprovalRequest`** (`status: "REJECTED"`). A discount reject reverts the draft
sale to `DRAFT`; a return reject changes nothing.

### Filtering `GET /api/v1/approvals/` (G21)

Validated query params, AND-combined, composing with `search` / `ordering` /
`page` / `page_size`:

* `?status=` — `PENDING` | `APPROVED` | `REJECTED` (exact)
* `?request_type=` — `DISCOUNT` | `RETURN` | `CORRECTION` | `STOCK_ADJUSTMENT` |
  `DAMAGE` (exact)

An unknown value is a `400 validation_error` (`field_errors.status` /
`field_errors.request_type`), never silently ignored. Owner sees the branch's
requests; a cashier still sees only their own. Use `?status=PENDING` for the
"Pending" tab so a request never hides on page 2.

## Discount approval state on a sale (G22)

`SaleRead` (every `GET /sales/{id}/`, and every draft / finalise / cancel
response, and each row of `GET /sales/`) now carries a read-only, **nullable**
`discount_request`:

```jsonc
"discount_request": null            // no DISCOUNT request has ever been raised for this sale
// or:
"discount_request": {
  "id": "uuid",
  "status": "PENDING" | "APPROVED" | "REJECTED",   // apps.sales ApprovalStatus — no CANCELLED/EXPIRED
  "requested_amount": "1500.00",                    // decimal string, always set
  "approved_amount": "1000.00" | null,              // decimal string once APPROVED, else null
  "reason": "string",
  "reviewer_note": "string",                        // "" until a decision; "Superseded: ..." on auto-void
  "requested_by_username": "string",
  "reviewed_by_username": "string" | null,          // null until an owner decides (and for auto-void)
  "reviewed_at": "2026-08-30T21:01:22+01:00" | null,
  "created_at": "2026-08-30T20:55:00+01:00"
}
```

* It is the **most recent** DISCOUNT `ApprovalRequest` for the sale (any
  status). Non-discount approvals (returns) never appear here.
* **Do not infer approval from `Sale.status` or `discount_total`** — read
  `discount_request.status`.
* Field names are the canonical model names: **`requested_amount`** /
  **`approved_amount`** (not `amount_requested`/`amount_approved`), `status`,
  `reason`, `reviewer_note`, `reviewed_at`, `created_at`.
* **Cancelled / expired is not a distinct state in the model.** A request voided
  by a `PUT /sales/{id}/draft-cart/` or `POST /sales/{id}/cancel/` becomes a
  `REJECTED` row with `reviewed_by_username: null` and a `reviewer_note` that
  starts `"Superseded: "`. Distinguish: `REJECTED` + `reviewed_by_username`
  **set** = an owner rejected it; `REJECTED` + `reviewed_by_username` **null**
  (note starts `"Superseded: "`) = auto-voided by a cart edit / cancel.
* **Visibility** = exactly who can already `GET` the sale: the sale's own
  cashier, and the branch owner. An unrelated cashier or another branch's owner
  gets `404` (unchanged) and therefore never sees the summary.
* No `fingerprint`, no `subtotal` snapshot, no raw user ids, no cost/profit.

**Exact `Sale.status` / `discount_total` behaviour (so the UI never guesses):**

| Event | `discount_request.status` | `Sale.status` | `Sale.discount_total` |
|---|---|---|---|
| no request | `discount_request == null` | `DRAFT` | `"0.00"` |
| cashier requests a discount | `PENDING` | `DRAFT` → `PENDING_APPROVAL` | `"0.00"` (unchanged) |
| **owner approves** | `PENDING` → `APPROVED` (`approved_amount` set, `reviewed_*` set) | **stays `PENDING_APPROVAL`** (approve does *not* move the sale; `finalise` does) | `"0.00"` → **`approved_amount`** |
| **owner rejects** | `PENDING` → `REJECTED` | `PENDING_APPROVAL` → **`DRAFT`** | → **`"0.00"`** |
| cashier finalises the approved draft | `APPROVED` (unchanged) | `PENDING_APPROVAL` → `COMPLETED` | `approved_amount` (re-derived, unchanged) |
| cart edited / draft cancelled while `PENDING` | `PENDING` → `REJECTED` (superseded, no reviewer) | → `DRAFT` / `CANCELLED` | → `"0.00"` |

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
   `{device, cashier}` → returns the `OfflineAuthorization` (`id`,
   `snapshot_version`, `issued_at`, `expires_at`, `signed_token`, …). The
   `signed_token` is the server-verified proof; the create/retrieve/list
   responses do **not** inline the snapshot rows. Valid ≤ 24 h.
3. **The bound cashier reads the snapshot** (needed to build sales while
   offline): `GET /api/v1/offline/authorizations/{id}/snapshot/` →

   ```jsonc
   {
     "authorization_id": "uuid",
     "status": "ACTIVE",                  // always ACTIVE on a 200
     "snapshot_version": 7,
     "issued_at":  "2026-08-29T09:00:00+01:00",
     "expires_at": "2026-08-30T09:00:00+01:00",
     "signed_token": "<opaque>",          // pass back as authorization_token
     "items": [
       {
         "variant_id": "uuid",
         "name": "Weathershield Exterior Paint",   // product display name
         "sku": "WS-WHT-20L",
         "price": "48500.00",             // exact Decimal string, 2 dp — FIXED
         "quantity": 12,                  // whole units at session open (snapshot info, not live stock)
         "low_stock_level": 6
       }
     ]
   }
   ```

   Exact field names — do not guess: item keys are **`name`** (not
   `product_name`), **`price`** (not `fixed_price`), **`quantity`** (not
   `stock`). Response is `Cache-Control: private, no-store`. Not paginated —
   it is a bounded per-branch active-catalogue set; read it once at session
   start. The snapshot is **frozen for the life of the session**: calling it
   again returns byte-identical data and never picks up a newer price or stock
   level. No cost / average-cost / stock-value / profit fields.

   **Device / cashier / branch binding proof.** The `GET` carries **no request
   body and no client-supplied token** — only the session cookie and the
   `{id}` in the path. The binding is proven entirely server-side: (1) the
   authorization row must belong to the caller's branch (else `404`); (2) the
   server re-verifies its **own stored** `signed_token` with the *same*
   `django.core.signing` HMAC-SHA256 + `constant_time_compare` check that
   `POST /offline/sync/` uses, and the verified payload's `branch_id` /
   `device_id` / `cashier_id` must match the row **and** the caller
   (`authorization.cashier_id == request.user.id`); (3) the bound device must
   still be the branch's one ACTIVE `RegisteredDevice`. The client supplies
   none of those values, so it cannot forge them.

   **404 vs 409 — the split is deliberate:**

   | Situation | Response |
   |---|---|
   | wrong cashier, wrong branch, unknown `{id}`, or the bound device is no longer the active registered device (revoked / replaced device) | `404` `not_found` — **indistinguishable**; never reveals whether a snapshot exists |
   | *your own* correctly-bound authorization, but **expired** | `409` `offline_authorization_expired` (standard envelope) |
   | *your own* correctly-bound authorization, but **revoked / replaced / force-closed / closed** | `409` `offline_session_not_active` (standard envelope) |
   | the stored signed token fails verification (tamper / key change) | `400` `invalid_signature` |

   So an *identity/existence* mismatch is a blind `404`; a *dead session that
   is provably yours* is a `409` you can show the cashier ("session expired —
   ask the owner to reopen").
4. While a session is ACTIVE the branch's online stock-changing endpoints
   return `409 offline_session_active`.
5. The device sells offline against the snapshot, decrementing **local** stock,
   printing a temporary receipt labelled
   `OFFLINE RECEIPT — PENDING SYNCHRONIZATION` (no official number). Optional
   preview: `POST /api/v1/offline/temporary-receipt/`.
6. When online: `POST /api/v1/offline/sync/`
   `{authorization_token, sales: [{client_sale_id, device_sequence,
   offline_created_at, items, payments, customer_name?, customer_phone?}]}`.
   The server recomputes every price/total from the signed snapshot. **Response
   (G23):** always **one object** — `{ "results": [OfflineSyncResult, …] }` —
   one entry per submitted sale in device-sequence order. **Never a bare
   array**, even for a one-sale batch. Each `OfflineSyncResult`:
   `{client_sale_id, device_sequence, outcome, detail_code, sale_id,
   receipt_number}`.
   * `outcome` — `ACCEPTED` (+ `sale_id`, `receipt_number`) · `DUPLICATE`
     (already synced; same `sale_id`/`receipt_number`) · `CONFLICT` (real sale,
     can't apply now — retained for the owner) · `REJECTED` (invalid payload —
     the device already took real money, so the owner must reconcile it) ·
     `OWNER_REVIEW_REQUIRED` (session was revoked/replaced/force-closed first).
   * `detail_code` — `""` for `ACCEPTED`/`DUPLICATE`; otherwise one of:

     | with `outcome` | `detail_code` values |
     |---|---|
     | `REJECTED` | `duplicate_sequence`, `outside_window`, `invalid_timestamp`, `empty_cart`, `price_not_in_snapshot`, `invalid_quantity`, `payment_required`, `invalid_payment_method`, `invalid_payment_amount`, `reference_required`, `invalid_tendered_amount`, `payment_mismatch`, `variant_not_found`, `variant_unavailable` |
     | `CONFLICT` | `stock_not_available`, `sale_in_progress` |
     | `OWNER_REVIEW_REQUIRED` | `revoked`, `replaced`, `force_closed` |

   Suggested wording: `payment_mismatch` → "the amount tendered on the device
   doesn't match the snapshot price"; `stock_not_available` → "stock ran out
   before this sale reached the server — owner to review"; `outside_window` →
   "sale timestamped outside the authorised session window"; `duplicate_sequence`
   → "two queued sales share a device sequence number"; `revoked`/`replaced`/
   `force_closed` → "the offline session was closed before this sale synced —
   owner to review". Retrying the whole batch is safe (idempotent per
   `client_sale_id`).
7. **`GET /api/v1/offline/sales/{client_sale_id}/`** — the bound cashier's
   device polls this to reconcile a queued sale after the owner acts. `200` is
   one object (`OfflineSaleLookup`):

   ```jsonc
   {
     "client_sale_id": "<uuid>",
     "device_sequence": 4,
     "outcome": "ACCEPTED" | "DUPLICATE" | "CONFLICT" | "REJECTED" | "OWNER_REVIEW_REQUIRED",
     "detail_code": "",                    // as in OfflineSyncResult
     "sale_id": "<uuid>" | null,           // set once an official Sale exists
     "receipt_number": "VS-…" | null,
     "resolved": true | false,             // owner has actioned the sync-record
     "resolved_at": "<iso8601>" | null,
     "resolution_note": ""                 // "" until resolved
   }
   ```

   Returned for a record in **any** outcome, not only "once synced". Reconcile
   logic: `sale_id` set (or `outcome` `ACCEPTED`/`DUPLICATE`) → it became an
   official sale; `resolved: true` with `outcome` `CONFLICT`/`REJECTED` and
   `sale_id: null` → the owner handled it **without** a Sale (the normal
   `REJECTED` path, and possible for `CONFLICT`) → clear the "needs the owner"
   row. **Binding:** readable only by the cashier of the authorization that
   submitted this `client_sale_id` (same binding as `/offline/sync/`); any other
   user (including the branch owner) or an unknown id → an indistinguishable
   `404 not_found`.
8. `POST /api/v1/offline/authorizations/{id}/end-session/`
   `{force?, mfa_confirmed?, reason?}` — **owner-only** (all variants). A
   **cashier** calling it gets `403 { "code": "permission_denied" }` ("Only the
   shop owner may perform this action.") — treat that as "ask the owner", not a
   transient error. A plain end needs **zero unresolved review-queue records**
   (see step 9); a force-end (with pending records) needs `mfa_confirmed: true`
   and records `FORCE_CLOSED`. There is no cashier-callable end-session.
9. Owner reviews `GET /api/v1/offline/sync-records/`. **The review queue =
   unresolved `CONFLICT` + `OWNER_REVIEW_REQUIRED` + `REJECTED` records** —
   for each, **no Sale, stock movement, payment, revenue or COGS exists yet**.
   Each blocks a plain `end-session` (`pending_review_count`) until it is
   reconciled. **G3 — validated filters** (owner-only, branch-scoped, compose
   with pagination / ordering / search): `?outcome=` (`ACCEPTED` | `DUPLICATE`
   | `CONFLICT` | `REJECTED` | `OWNER_REVIEW_REQUIRED`) and `?resolved=`
   (`true` | `false`). An unknown value is a `400 validation_error`. Use
   `?resolved=false` for the whole review queue.

   **`POST /api/v1/offline/sync-records/{id}/resolve/` `{note}` is deprecated**
   — it returns `409 offline_reconciliation_required` for every live queue
   record. A note cannot put stock / revenue / COGS right.

   **`POST /api/v1/offline/sync-records/{id}/reconcile/`** — owner + MFA — is
   the safe resolution. Polymorphic on `kind` (see **Offline sale
   reconciliation** below). Cross-branch / unknown id → `404 not_found`.
   Success: `200 { "sync_record": OfflineSyncRecord, "reconciliation":
   OfflineReconciliation }`; `sync_record.resolved` is now `true` and it leaves
   `pending_review_count`.
10. Client submits `device_sequence` strictly increasing; **never** submit a
   client-computed price or total — they are ignored.

## Offline sale reconciliation

`POST /api/v1/offline/sync-records/{id}/reconcile/` — **owner + MFA**, one per
record, immutable once done. `kind` picks the path:

### `RECORDED_AS_SALE` — the customer kept the goods and the shop kept the payment

```jsonc
{ "kind": "RECORDED_AS_SALE",
  "explanation": "<string, >= 10 chars>",
  "counts": [ { "variant": "<uuid>", "counted_on_hand": <int >= 0> } ],
  "payment_references": [ { "payment_index": <int >= 0>, "reference": "<string>" } ] }
```

The backend re-reads the **retained payload** + the **verified signed
snapshot** and creates the official Sale itself — server-authoritative prices,
COGS (pre-existing weighted-average cost), one receipt, `source: OFFLINE`,
`Payment.offline_confirmed: true`. You **never** resend lines, prices, amounts
or the customer. It preserves the original `offline_created_at` when that time
is inside the authorised window, else uses "now"
(`reconciliation.completion_time_substituted`). The Sale is only ever built
from a snapshot-verified total, so `reconciliation.amount_source` is always
`SNAPSHOT_VERIFIED` for this kind.

* `counts` — one row **per affected variant**, exactly once. **Required** when
  `outcome == CONFLICT` (and whenever stock is short: you get
  `409 physical_count_required`, resubmit with counts). `counted_on_hand` is
  what is physically on the shelf **now**, after the offline goods left. The
  backend records `pre_sale = counted_on_hand + qty_in_sale` as an
  `OFFLINE_RECONCILIATION` stock correction (not a restock — cost basis
  unchanged), then the normal sale deduction; final DB balance ends at
  `counted_on_hand`, never negative.
* `payment_references` — **structured**, one entry per **retained Transfer/POS**
  payment. `payment_index` is the 0-based position in the record's
  `retained_payments` (see below), so a split Transfer + POS batch is never
  mis-mapped by order. Missing an entry for a required line →
  `400 reference_required`; unknown index → `payment_reference_index_invalid`;
  same index twice → `payment_reference_duplicated`; a reference against a CASH
  line → `payment_reference_unexpected`. CASH-only records need none.

**`retained_payments` (read model on `GET .../sync-records/{id}/`, owner-only)** —
`[{ "payment_index", "method", "amount", "reference_required" }]`. It gives the
owner exactly what they need to key the correct slip number against each line
(index, method, amount) and **never** carries a reference — the device's
original Transfer/POS reference was discarded at sync time.

### `REFUNDED_AND_RETURNED` — the customer returned everything, refunded in full

```jsonc
{ "kind": "REFUNDED_AND_RETURNED",
  "explanation": "<string, >= 10 chars (>= 40 when owner_attestation)>",
  "all_goods_returned": true,
  "full_amount_refunded": true,
  "owner_attestation": false,           // true when the collected amount can't be established
  "attested_offline_total": "<decimal>",   // required when owner_attestation is true
  "refunds": [ { "method": "CASH"|"TRANSFER"|"POS", "amount": "<decimal>", "reference": "<string>" } ] }
```

Both flags must be `true`; **`refunds` must total the money _actually
collected_ from the customer — NOT the catalogue total**; Transfer/POS needs a
`reference`. **No Sale, Payment, SaleReturn or stock movement is created** —
only immutable refund evidence (`reconciliation.refunds`,
`reconciliation.refund_total`). Anything partial →
`409 partial_reconciliation_unsupported` (record the sale, then use the normal
`POST /api/v1/sales/{id}/return-requests/` flow).

How the collected amount is established (`reconciliation.amount_source`):

* **`RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`** — the retained device payment
  total parsed cleanly **and equals** the cryptographically verified snapshot
  total, so the collected amount is that agreed figure. No attestation needed.
* **`OWNER_ATTESTED`** — the retained payments disagree with the verified
  snapshot (e.g. a `payment_mismatch` REJECTED record where the device recorded
  ₦1,999 against a ₦2,000 catalogue total), **or** the snapshot can't be
  verified at all (broken signature / binding, malformed items). The backend
  will **not** guess: without `owner_attestation: true` you get
  `409 offline_total_unverifiable`. Resubmit with `owner_attestation: true` +
  `attested_offline_total` = **the amount actually collected** (from till
  roll / drawer count) + a `explanation` of **≥ 40 chars**. `refunds` must
  then total that attested amount exactly. Missing figure →
  `attested_total_required`; thin explanation →
  `attestation_explanation_too_short`.

If the established collected amount is **not positive** — nothing was actually
taken — the record is **not a refund**: `409 no_payment_to_refund`. Do not
submit fabricated positive `refunds` for it. **Known limitation:** a corrupted
zero-payment record currently has *no* reconciliation path (nothing to charge,
nothing to refund, no sale to link) — it stays unresolved in the review queue
and keeps blocking a plain `end-session` until a future explicit
no-transaction / void resolution type or controlled administrative handling is
added. Surface it to the owner as "needs head-office" rather than retrying.

**Owner-only diagnostics** on the response for an `OWNER_ATTESTED` refund:
`reconciliation.verified_snapshot_total` and
`reconciliation.retained_payments_total` (either may be `null`) show the
verified catalogue total and the retained device payment total side by side, so
the owner can see why the amounts diverged. They are **absent** from the
cashier `OfflineSaleLookup` and are **never** written to an audit-log row.

### `LINKED_EXISTING_SALE` — fallback only

```jsonc
{ "kind": "LINKED_EXISTING_SALE", "explanation": "<string>",
  "owner_attestation": false,   // required true only when nothing is comparable
  "sale_id": "<uuid>"  /* or */  "receipt_number": "<string>" }
```

Allowed when the retained data can't be cryptographically verified
(`offline_data_untrusted`) and the owner has re-entered the sale manually. The
target must be a **COMPLETED sale in this branch**, not already linked
(`sale_already_linked`). The backend then compares the target against the
retained record across **three dimensions**:

1. **line items and their quantities**,
2. **payment methods and amounts**,
3. **the transaction total** (verified snapshot figure, else the retained
   payment sum).

Any dimension that *can* be compared and mismatches → `409 sale_incompatible`
(so an unrelated same-branch sale can't be attached just because the branch and
total happen to line up). A cross-branch or unknown `sale_id` → `404 not_found`
(indistinguishable).

`reconciliation.link_verification` reports how thorough the match was:
`FULL` (all three dimensions were comparable and every one matched),
`PARTIAL` (every comparable dimension matched, but fewer than three could be
compared), or `MANUAL_ATTESTED` (**no** dimension was comparable — requires
`owner_attestation: true` + a ≥ 40-char `explanation`;
`amount_source: OWNER_ATTESTED`). `reconciliation.linked_manually` is always
`true` for this kind.

### Response — `OfflineReconciliationResult`

```jsonc
{ "sync_record": { ...OfflineSyncRecord, "resolved": true,
    "retained_payments": [ {payment_index, method, amount, reference_required} ] },
  "reconciliation": {
    "kind": "...", "resolved_by_username": "...", "resolved_at": "...",
    "explanation": "...", "sale_id": "<uuid>|null", "receipt_number": "...|''",
    "refund_total": "<decimal>|null",   // REFUNDED_AND_RETURNED: the amount actually collected
    "amount_source": "SNAPSHOT_VERIFIED"|"RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT"|"OWNER_ATTESTED"|"",
    "link_verification": "FULL"|"PARTIAL"|"MANUAL_ATTESTED"|"",
    "linked_manually": bool, "completion_time_substituted": bool,
    "verified_snapshot_total": "<decimal>|null",   // owner-only diagnostics (OWNER_ATTESTED refund)
    "retained_payments_total": "<decimal>|null",
    "refunds": [ {method, amount, reference} ], "counts": [ {variant, counted_on_hand, correction_delta, ...} ] } }
```

`amount_source` values: `SNAPSHOT_VERIFIED` (RECORDED_AS_SALE /
LINKED_EXISTING_SALE — the *sale* total recomputed from the verified snapshot),
`RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT` (REFUNDED_AND_RETURNED — retained
device payments agreed with that total), `OWNER_ATTESTED` (owner-attested
amount actually collected).

Owner + MFA only (has amounts / counts). **`reconciliation.refunds[].reference`
is the slip number the owner just typed and IS returned here** so the owner can
confirm it — it, and the `verified_snapshot_total` / `retained_payments_total`
diagnostics, are excluded from every audit-log row and from the cashier read
model. The **cashier's device** reads only the safe status from
`GET /api/v1/offline/sales/{client_sale_id}/`, which carries `resolution_kind`
(`RECORDED_AS_SALE` | `REFUNDED_AND_RETURNED` | `LINKED_EXISTING_SALE` | `null`)
alongside `resolved` / `sale_id` / `receipt_number` — **no `refunds`**, no
amounts, no cost.

**Stable error codes**: `offline_reconciliation_required`,
`offline_record_not_reconcilable`, `offline_record_already_resolved`,
`physical_count_required`, `count_variant_missing`, `count_variant_unexpected`,
`count_variant_duplicated`, `invalid_count`, `reference_required`,
`payment_reference_index_invalid`, `payment_reference_duplicated`,
`payment_reference_unexpected`, `payment_mismatch`, `refund_total_mismatch`,
`refund_required`, `invalid_refund_amount`, `no_payment_to_refund`,
`offline_total_unverifiable`, `attested_total_required`,
`attestation_explanation_too_short`,
`partial_reconciliation_unsupported`, `offline_data_untrusted`,
`sale_reference_required`, `sale_already_linked`, `sale_incompatible`,
`manual_verification_required`, `not_found`.

## Reports: best-sellers / slow-movers row cap (G13 / G18)

`GET /api/v1/reports/best-sellers/` and `/api/v1/reports/slow-movers/` return a
**bare JSON array** — *not* a `{count, results}` envelope. They do **not**
paginate: `?page` / `?page_size` are ignored. `?limit=<1..100>` is the only row
cap (default `10` for best-sellers, `20` for slow-movers); out-of-range values
clamp. `?period` / `?start` / `?end` select the window as for `reports/profit`.
`reports/profit` and `reports/inventory` each return a single summary object.

## Reading the branch timezone (G12)

`Branch.timezone` (IANA name, e.g. `Africa/Lagos`) is on
`GET /api/v1/branches/{id}/` — readable by **any** authenticated (MFA-cleared)
branch user, not just the owner (`IsOwnerOrReadOnly`); the queryset is
branch-scoped so a user only sees their own branch. `/auth/me/` gives
`branch` (id), `branch_code`, `branch_name` but not the timezone — fetch the
branch record for that. All timestamps already carry a `+01:00` offset;
business-day boundaries are the branch timezone.

## Customer phone masking (G14)

| Call | Owner sees | Employee sees |
|---|---|---|
| `GET /customers/` (list) | full number | **masked** (`*******1234`) |
| `GET /customers/{id}/` (retrieve) | full number | **full number** |
| `POST /customers/` / inline on a sale | echoes what was stored (full) | echoes (full) |

Only the broad **list** is masked, and only for employees. Never log a customer
phone (see the security note). This is existing behaviour — documented, not
changed.

## Development CORS setup

`config/settings/development.py` allows `http://localhost:3000` and
`http://127.0.0.1:3000` with credentials. To use another dev origin:

```bash
CORS_ALLOWED_ORIGINS=http://localhost:5173 CSRF_TRUSTED_ORIGINS=http://localhost:5173 \
  .venv/Scripts/python manage.py runserver
```

Credentialed CORS requires **exact** origins — no wildcards. The SPA must send
`credentials: 'include'` and the `X-CSRFToken` header on writes.
