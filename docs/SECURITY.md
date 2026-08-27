# Security model & decisions

This is the reviewed security posture of the Viable Stone backend as frozen at
Stage 18. Every statement here is backed by an automated acceptance test under
`tests/acceptance/`; the test name is given in parentheses.

Nothing here describes a deployment. External alerting, WAFs, private-network
placement and TLS termination are the operator's responsibility and are **not**
claimed as active.

---

## 1. Production admin & debug policy

* The Django admin is a **development convenience only**. It is mounted only when
  `ADMIN_ENABLED` is true; `config/settings/production.py` sets it `False`, so
  under production settings `/admin/`, `/admin/login/` and every sub-path return
  **404** for anonymous *and* authenticated users
  (`test_admin_debug_policy.py::TestAdminRouting`).
* There is **no** secret alternative admin URL. The URLconf contains exactly one
  admin include, gated on the flag; a resolver walk asserts nothing else
  matches `admin` when disabled (`test_no_alternate_admin_url_exists`).
* `DEBUG=False` in production (`test_debug_is_false_in_production_settings`).
* No debugging/profiling package is installed or routed: `debug_toolbar`,
  `silk`, `django_extensions`, `nplusone`, `django_cprofile_middleware`
  (`TestDebugTooling`). No `__debug__/` or `silk/` route exists.
* OpenAPI schema/docs remain behind the Stage 17 gate: owner **or**
  tech-admin, and disabled entirely when `API_DOCS_ENABLED=False`
  (`TestApiDocsPolicyUnchanged`). Under production settings `API_DOCS_ENABLED`
  defaults to `False`.
* Health endpoints expose only `{"status": ...}` and per-check states — no
  version, dependency URL, credential, path or stack trace
  (`TestHealthLeakage`).

## 2. Authentication & authorisation

* Cookie-session auth only (`cookieAuth` is the sole advertised scheme in
  `openapi.yml`). No bearer tokens.
* Every `/api/v1/` route carries a **reviewed security classification** in
  `tests/acceptance/security_classification.py`. `test_route_inventory.py`
  fails the build if a new route appears without one, if the classification
  lists a route that no longer exists, or if the set of `public`-tagged routes
  drifts from `PUBLIC_ALLOWLIST`.
* **Public allowlist** (the only unauthenticated endpoints):
  `api:health`, `api:health-live`, `api:health-ready`, `auth:csrf`,
  `auth:login`. Everything else requires a session.
* Acceptance matrix (`test_authz_matrix.py`):
  * unauthenticated → `401` with the standard envelope
    (`code` in `not_authenticated` / `authentication_failed`);
  * wrong role → `403 permission_denied` (employee on owner endpoints;
    tech-admin on business data);
  * another branch's object id → `404` (never `403`, never data);
  * owner/tech-admin privileged operations require a **confirmed-MFA session**
    (`session["mfa_verified"]`); without it, `403`;
  * state-changing session requests enforce **CSRF** (`X-CSRFToken`);
  * a deactivated user's live session stops working on the next request;
  * a revoked offline device/authorization cannot obtain new authorisations or
    create offline sales;
  * employee-facing responses never contain `unit_cost`, `average_unit_cost`,
    `unit_cost_snapshot`, `profit`, `cogs`, push keys, `signed_token`,
    password or `code_hash` (`TestEmployeeOutputHasNoOwnerFields`).

## 3. Rate limiting

* Global baseline: `AnonRateThrottle` + `UserRateThrottle` + `ScopedRateThrottle`
  are the DRF defaults (`test_rate_limiting.py::TestThrottleConfiguration`).
  Defaults: `anon` 120/min, `user` 2000/min (overridable by env).
* Sensitive scopes are attached and cannot silently disappear (a parametrized
  test pins each view → scope): `auth_login`, `auth_mfa`, `auth_recovery`,
  `sales_write`, `notifications_write`, `offline_sync`.
* Health probes are **not** throttled (`throttle_classes == []`), so infra can
  poll them.
* A throttled response uses the standard envelope (`code: "throttled"`) with a
  `Retry-After` header.
* A `429` writes a scrubbed `apps.security` line (`event: rate_limited`) — no
  request body, credential, token, payment reference or phone number
  (`test_429_logs_a_scrubbed_security_event`).
* Production uses Redis (`django_redis.cache.RedisCache`), not local-memory,
  for throttle counters (`test_production_throttling_uses_redis_not_locmem`).

## 4. Input validation & safe output

**Unknown-field policy — documented and consistent:** unknown / read-only /
server-computed fields in a request body are **silently ignored**, never
applied. DRF drops them on the way in; the server never trusts a client value
for an id, a timestamp, a price, a total, a discount, a receipt number or a
branch (`test_input_validation.py::TestUnknownAndComputedFields`). This is a
deliberate choice over "reject unknown fields" so that a newer frontend sending
an extra field to an older backend does not hard-fail.

* Length / type / choice / UUID / date / money validation is enforced by the
  serializers; quantities are positive whole numbers; money must be a valid
  decimal and expense amounts must be `> 0`
  (`TestPrimitiveValidation`).
* Prices, totals, COGS, discounts, receipt numbers, weighted-average costs and
  branch ownership are always computed or verified **server-side**
  (`test_client_cannot_set_price_total_or_receipt_number_on_a_sale`,
  and the offline-sync journey which recomputes everything from the signed
  snapshot).
* **NUL bytes** are rejected (DRF `ProhibitNullCharactersValidator`).
  **Other C0/C1 control characters** are rejected by
  `ControlCharSafeSerializerMixin`, applied to every write serializer; tab and
  newline are allowed (`test_other_control_characters_are_rejected`,
  `test_tab_and_newline_are_allowed`). There is **no** regex "HTML sanitiser"
  and legitimate business text is never mutated.
* Uploads (`Expense.receipt_file`) are validated by **extension + declared MIME
  + magic bytes + size** (`apps/core/validators.py::validate_private_upload`).
  A shell script renamed `.pdf` is rejected with `receipt_file` in
  `field_errors` and a `rejected_upload` security-log line
  (`TestUploadValidationAtTheApi`).
* All queries use the Django ORM (parameterised). Search filters use
  `icontains`, which escapes SQL `LIKE` wildcards, so `' OR '1'='1`, `%`, `_`
  and `'; DROP TABLE …` are treated as literal text and match nothing; the
  tables still exist afterwards (`TestSqlInjection`).
* User-controlled text is treated as **plain text**. The JSON APIs return it
  verbatim (JSON-encoded); the ReportLab PDF path escapes it with
  `apps/core/text.py::pdf_escape` before it reaches a `Paragraph`, so
  `<script>`, `"><img onerror=…>`, `javascript:` and stray markup render as
  inert characters and never crash or inject the PDF
  (`TestXssAndPdfRendering`).

> **Frontend rule:** render every API string as text. Never pass an API value
> to `innerHTML`, `dangerouslySetInnerHTML`, `v-html`, `document.write`, or a
> template that does not auto-escape. Use `textContent` / framework text
> interpolation. See `FRONTEND_HANDOFF.md`.

## 5. Database security decision — no blanket Row-Level Security

**Decision (Stage 18): Viable Stone does not adopt blanket PostgreSQL Row-Level
Security.** Branch isolation is enforced by, in depth:

1. Django role permissions (`IsOwner`, `IsOwnerOrTechAdmin`,
   `IsAuthenticatedAndMFAVerified`, …);
2. branch-scoped querysets — `BranchScopedQuerysetMixin` or an explicit
   `get_queryset()` filter by `request.user.branch_id` on every branch-owned
   viewset;
3. locked services (`select_for_update`) that re-check branch ownership;
4. database foreign keys, `CHECK`s and unique constraints
   (e.g. `(branch, client_sale_id)`);
5. the branch-isolation acceptance matrix (`test_branch_isolation.py`), which
   creates one row per branch-owned resource in branch A and asserts that an
   owner of branch B gets `404` on every detail route and never sees an A row
   in any list; `test_matrix_covers_every_branch_scoped_route` fails if a
   `branch_scoped` route is added without coverage.

Supporting notes:

* The `public` **schema name** is a PostgreSQL namespace. It does **not** make
  tables internet-reachable; reachability is a network/credential matter.
* Production PostgreSQL must be on a private network and credential-protected.
* The production application role must **not** be `SUPERUSER`, `CREATEROLE`,
  `CREATEDB` or `BYPASSRLS`. A local *test* role may have `CREATEDB` only, so
  `pytest` can create its isolated test database.
* PostgreSQL RLS remains a documented **future** defence-in-depth option. It
  would need a separate design for a per-connection branch context
  (`SET app.current_branch`) and policies per table; it is deliberately out of
  scope for Stage 18.

## 6. Safe errors & security logging

* Every API error is the envelope
  `{code, message, field_errors, request_id}` (`request_id` is also the
  `X-Request-ID` header). Covered categories, each asserted in
  `test_safe_errors_and_logging.py`: malformed JSON (`parse_error` 400),
  validation (`validation_error` 400), auth failure (`401`), permission denied
  (`403`), cross-branch / not found (`404`), idempotency conflict (`409`),
  rate limit (`429`), unhandled exception (generic `server_error` 500).
* **No** error body contains a stack trace, filesystem path, SQL, database URL,
  environment variable, secret key/token or internal exception message
  (`TestNoResponseLeaksInternals`; the forced-500 test asserts the `RuntimeError`
  message, `SELECT …` and `/srv/...` are all absent).
* Structured `apps.security` logging (JSON in production via
  `apps/core/logging.py::JSONFormatter`) for:
  repeated auth failure (`auth_failed`) and django-axes lockout
  (`axes_lockout`); rate limiting (`rate_limited`); permission denial
  (`permission_denied`); invalid offline signature (`invalid_offline_signature`);
  rejected upload (`rejected_upload`); unexpected server error (`server_error`).
  Every line keeps the coordinates for an investigation (path, method,
  username, client IP, `request_id`, `user_id`) and **nothing** from the
  request body — no password, token, payment reference or phone number
  (`TestStructuredSecurityLogging`).
* Sentry is optional and inert without `SENTRY_DSN`. When configured,
  `apps/core/sentry.py::scrub_event` is wired as `before_send` /
  `before_send_transaction`, `send_default_pii=False`,
  `max_request_body_size="never"`; it strips cookies, auth/CSRF headers,
  request bodies, query strings and any key that looks like a secret, phone,
  payment reference, push key or offline token, and preserves `request_id`
  (`TestSentryScrubbingStillWorks`).
* **External alerting is not active.** It requires deployment configuration.

---

## Running the acceptance suite

```bash
pytest tests/acceptance/ -q                 # all Stage 18 acceptance tests
pytest tests/acceptance/test_branch_isolation.py -q
pytest -m concurrency -q                    # existing race-condition coverage
```

See `FINAL_ACCEPTANCE.md` for the full automated + manual checklist and the
security decision matrix.
