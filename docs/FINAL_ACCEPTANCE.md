# Final acceptance checklist — Stage 18

The backend is feature-complete. This document is the acceptance record: what is
verified automatically, what a human must still check on a running instance, and
what can only be proven by GitHub Actions.

- **Automated suite:** 675 tests collected, **670 passed, 5 skipped**
  (the 5 are real-Redis integration tests, skipped locally because no Redis is
  running; GitHub CI runs them with zero skips).
- **Coverage:** 94 % (gate: ≥ 90 %).
- **OpenAPI:** `openapi.yml` — 100 paths, 141 operations, 160 component schemas;
  regenerates with **no diff** and **zero warnings**.

> **Post-Stage-18 additive correction (2026-08-28):** secure product-image
> delivery — `GET`/`DELETE /api/v1/products/{id}/image/` (authenticated,
> branch-scoped, streamed through the storage backend) and an additive
> `image_url` field on product read responses; signature-checked JPEG/PNG/WebP
> uploads (5 MB); `variants/?search=` widened to name / SKU / barcode / colour /
> size / finish / brand / category. Root cause: the private image had no
> serving route at all (`MEDIA_URL` was unrouted). 39 new tests
> (`apps/catalog/tests/test_product_images.py`, `test_variant_search.py`);
> contract snapshot + security classification + branch-isolation matrix updated
> in step. No model/migration change.

---

## A. Automated acceptance — `tests/acceptance/`

| Spec item | Suite | What it proves |
|---|---|---|
| 1 — Admin & debug policy | `test_admin_debug_policy.py` (11) | `/admin/` 404s under production settings for anon + authed; no alternate admin URL; `DEBUG=False`; no debug/profiling app or route; docs gate unchanged; health bodies minimal. |
| 2 — Auth/authz matrix | `test_route_inventory.py` (4), `test_authz_matrix.py` (48, parametrized) | every route classified (build fails on an unclassified new route); public allowlist pinned; 401/403/404 semantics; MFA-required; CSRF on writes; disabled users & revoked devices stop working; no owner-only fields in employee output. |
| 3 — Rate limiting | `test_rate_limiting.py` (12) | global anon/user baseline + `ScopedRateThrottle`; sensitive scopes attached per view; health probes unthrottled; safe 429 envelope + `Retry-After`; scrubbed `rate_limited` security log; Redis in production. |
| 4 — Input validation & safe output | `test_input_validation.py` (14) | unknown/computed fields ignored; primitive/money/UUID/date/quantity validation; NUL + control chars rejected (tab/newline allowed); upload extension+MIME+magic+size; SQL-injection payloads inert; XSS/markup escaped in the PDF, verbatim in JSON. |
| 5 — Branch isolation (no RLS) | `test_branch_isolation.py` (5) | one row per branch-owned resource in branch A; outsider owner gets 404 on every detail route and sees no A rows in any list; inventory + profit report branch-scoped; coverage-of-coverage guard. |
| 6 — Safe errors & security logging | `test_safe_errors_and_logging.py` (21) | every error category → standard envelope + `request_id`; no stack trace / path / SQL / DB URL / secret / internal message in any body; structured `apps.security` logs for auth failure, axes lockout, rate limit, permission denial, invalid offline signature, rejected upload, 500 — all scrubbed; Sentry `scrub_event` still wired. |
| 7 — End-to-end business journeys | `test_business_journeys.py` (15) | owner auth+MFA; branch/role isolation; catalogue + pricing + price history; restock + weighted-avg cost + movements; fully-paid cash sale + receipt (JSON + PDF); split-payment sale; idempotent retry (one sale/payment/stock move); owner-approved discount + finalise; resellable + damaged returns (refund, stock, COGS reversal); protected adjustment + audit trail; expenses + profit/best-seller/slow-mover/inventory reports; low-stock notification + dedup; offline authorisation + signed snapshot + idempotent sync + retained conflict; cross-branch denial through a whole workflow; snapshot stability after name/price change. |
| 8 — Frozen OpenAPI contract | `test_openapi_contract.py` (10) | path/method + operationId surface matches a committed snapshot (drift fails the build); operationIds unique; every path under `/api/v1/`; only `cookieAuth`; list endpoints use the paginated wrapper; money fields are decimal strings; enums documented; no server secret in the schema; regeneration is byte-stable with `--fail-on-warn`. |

Snapshot file: `tests/acceptance/openapi_contract_snapshot.json` — regenerate
alongside `openapi.yml` whenever a route change is intentional.

### Defects found and fixed during Stage 18

1. **`POST /api/v1/products/` and `/api/v1/variants/` returned `500`.**
   `AuditCreateUpdateMixin` stored `serializer.data` in the `AuditLog` JSON
   field; product/variant payloads contain `UUID` related-pks, which psycopg
   cannot adapt inside a `JSONField`. Fixed in
   `apps/core/services/audit.py` — `record_audit` now coerces `before`/`after`
   to JSON-native types (`UUID`/`Decimal` → str, dates → ISO). Regression
   covered by `test_journey_catalogue_pricing_and_history`.
2. **Failed logins were not logged as security events.** django-axes lockout
   was logged, but the individual `invalid_credentials` / `account_disabled`
   rejections that lead up to it were not. Added a scrubbed `auth_failed`
   `apps.security` line in `LoginView` (username + client IP + path only, never
   the password). Covered by
   `test_repeated_auth_failure_is_logged_without_the_password`.

No behaviour was weakened to make a test pass.

---

## B. Manual acceptance — on a running instance

These need a browser / real infrastructure and are **not** automated.

- [ ] `docker compose up` (local Redis), `manage.py runserver`; log in as an
      owner in the browser, complete MFA, confirm the session cookie is
      `HttpOnly` + `SameSite=Lax` and the CSRF cookie is readable.
- [ ] With `DJANGO_SETTINGS_MODULE=config.settings.production` (and dummy env),
      `manage.py check --deploy --fail-level WARNING` is clean, and a request to
      `/admin/` returns 404.
- [ ] Generate a receipt PDF for a real sale and open it in a PDF viewer:
      layout, multi-page footer, `NGN` currency, no cost/profit anywhere.
- [ ] Web Push: subscribe a real browser via
      `GET /api/v1/push-subscriptions/public-key/` + `POST …/`, trigger a
      low-stock alert, confirm the push arrives and the in-app list matches.
- [ ] Offline flow end to end on a second device: issue authorisation, pull the
      snapshot, sell offline, reconnect, `POST /offline/sync/`, confirm official
      receipt numbers and that a deliberate oversell lands in
      `/offline/sync-records/?outcome=CONFLICT` for owner review.
- [ ] Backup/restore drill (`scripts/backup_restore_drill.sh`) against a real
      PostgreSQL 18 with `pg_dump`/`pg_restore` installed.
- [ ] Point a staging frontend at the API; verify credentialed CORS, CSRF on
      writes, and that all displayed strings are rendered as text.

---

## C. Final security decision matrix

| Area | Decision | Enforced by | Future option |
|---|---|---|---|
| Django admin in production | **Disabled** — `/admin/` 404s, no alternate URL | `ADMIN_ENABLED=False`, conditional URLconf include | — |
| Debug/profiling tooling | **Not installed, not routed** | `INSTALLED_APPS` / `MIDDLEWARE` / URL assertions | — |
| API docs/schema | Owner **or** tech-admin + gate; off by default in prod | `IsOwnerOrTechAdmin`, `API_DOCS_ENABLED` | — |
| Public endpoints | **Explicit allowlist** of 5; build fails on drift | `security_classification.py` + `test_route_inventory.py` | — |
| Cross-branch access | Guessed id → **404**, never 403 or data | permissions + branch-scoped querysets + FKs + acceptance matrix | per-connection RLS |
| MFA | Required for owner/tech-admin privileged ops | `IsAuthenticatedAndMFAVerified`, `session["mfa_verified"]` | WebAuthn |
| CSRF | Enforced on all session writes | Django CSRF + `enforce_csrf` on login | — |
| Rate limiting | Global baseline + per-scope; Redis in prod | DRF throttles; `test_rate_limiting.py` | adaptive/tarpit |
| Unknown request fields | **Silently ignored**, never trusted | DRF default + server-side recompute | — |
| Control characters | NUL + C0/C1 rejected; tab/newline allowed | DRF validator + `ControlCharSafeSerializerMixin` | — |
| User text → HTML/PDF | Plain text; escaped before ReportLab; verbatim in JSON | `apps/core/text.py::pdf_escape` | — |
| Uploads | extension + MIME + magic bytes + size | `validate_private_upload` | AV scan on ingest |
| Database RLS | **Not adopted** at Stage 18 | Django permissions + branch querysets + locked services + constraints + acceptance matrix | PostgreSQL RLS (defence in depth) |
| Production DB role | Not `SUPERUSER`/`CREATEROLE`/`CREATEDB`/`BYPASSRLS` | operator provisioning (documented) | — |
| Error responses | Standard envelope; no internals leaked | `apps/core/exceptions.py` + acceptance tests | — |
| Security logging | Structured `apps.security` JSON, scrubbed | `apps/core/logging.py`, `apps/core/exceptions.py`, `apps/accounts/auth.py` | ship to SIEM |
| Sentry | Optional, inert without DSN; PII-scrubbed | `apps/core/sentry.py::scrub_event` | — |
| External alerting | **Not active** — needs deployment config | — | PagerDuty/Slack on deploy |

---

## D. Still awaiting GitHub Actions / Docker (cannot be run locally)

Neither Docker nor a local Redis is available in this environment, so the
following are **not** verified here and must be confirmed by a CI run:

- [ ] Full pytest suite in CI with **0 skips** (the 5 real-Redis tests must run
      and pass).
- [ ] `pytest -m redis` — exactly 5 tests, 0 skipped, 0 failed.
- [ ] Production `Dockerfile` build + non-root smoke + `/health/ready/` → 503
      when a dependency is down.
- [ ] Trivy image scan (HIGH/CRITICAL, `ignore-unfixed: false`,
      empty `.trivyignore`).
- [ ] `pip-audit --strict` in CI (locally: no vulnerabilities, editable package
      skipped).

Locally verified and green: `pytest --collect-only`, full suite on real
PostgreSQL with `--create-db` + coverage ≥ 90, `ruff check`, `ruff format
--check`, `manage.py check`, `makemigrations --check`, `check --deploy
--fail-level WARNING` under production settings, `spectacular --validate
--fail-on-warn` + staleness, secret scan, GitHub Actions SHA-pin audit,
workflow/compose YAML validation.
