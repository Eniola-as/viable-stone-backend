"""The reviewed security classification of every ``/api/v1/`` route.

Every DRF route name resolvable under ``/api/v1/`` MUST have an entry here.
``tests/acceptance/test_route_inventory.py`` fails if a new route appears
without one, so a route can never ship unclassified.

Tags
----
public            : no authentication required (deliberate)
authenticated     : a logged-in session (any role)
mfa               : owner/tech-admin sessions must have cleared MFA
owner             : branch owner only
tech_admin_or_owner : owner or the technical administrator
branch_scoped     : results/objects limited to the caller's branch; other
                    branches' ids return 404
recipient_scoped  : limited to the calling user's own rows (notifications)
user_scoped       : limited to the calling user's own rows (push subs)
device_authorised : additionally gated by a signed offline authorization token
throttle:<scope>  : a ScopedRateThrottle scope is attached
docs_gated        : open in dev, owner/tech-admin-only or disabled in prod
write             : has state-changing methods (CSRF enforced for sessions)
read              : safe methods
"""

from __future__ import annotations

# Route names that are intentionally reachable without authentication.
PUBLIC_ALLOWLIST = {
    "api:health",
    "api:health-live",
    "api:health-ready",
    "auth:csrf",
    "auth:login",
}

CLASSIFICATION: dict[str, set[str]] = {
    # --- health / probes -------------------------------------------------- #
    "api:health": {"public", "read"},
    "api:health-live": {"public", "read"},
    "api:health-ready": {"public", "read"},
    # --- API documentation --------------------------------------------- #
    "api:schema": {"docs_gated", "tech_admin_or_owner", "read"},
    "api:docs": {"docs_gated", "tech_admin_or_owner", "read"},
    "api:redoc": {"docs_gated", "tech_admin_or_owner", "read"},
    # --- authentication --------------------------------------------------- #
    "auth:csrf": {"public", "read"},
    "auth:login": {"public", "write", "throttle:auth_login"},
    "auth:logout": {"authenticated", "write"},
    "auth:me": {"authenticated", "read"},
    "auth:password-change": {"authenticated", "write"},
    "auth:mfa-setup": {"authenticated", "write"},
    "auth:mfa-setup-confirm": {"authenticated", "write"},
    "auth:mfa-verify": {"authenticated", "write", "throttle:auth_mfa"},
    "auth:mfa-recovery": {"authenticated", "write", "throttle:auth_recovery"},
    "auth:mfa-recovery-regenerate": {
        "authenticated",
        "mfa",
        "write",
        "throttle:auth_recovery",
    },
    # --- accounts ------------------------------------------------------- #
    "branch-list": {"authenticated", "read", "owner", "write"},
    "branch-detail": {"authenticated", "read", "owner", "write"},
    "user-list": {"owner", "branch_scoped", "read", "write"},
    "user-detail": {"owner", "branch_scoped", "read", "write"},
    "user-activate": {"owner", "branch_scoped", "write"},
    "user-deactivate": {"owner", "branch_scoped", "write"},
    # --- core ---------------------------------------------------------- #
    "audit-log-list": {"tech_admin_or_owner", "branch_scoped", "read"},
    "audit-log-detail": {"tech_admin_or_owner", "branch_scoped", "read"},
    # --- catalogue (read: any authed; write: owner) ------------------- #
    **{
        name: {"authenticated", "read", "owner", "write", "branch_scoped"}
        for name in (
            "category-list",
            "category-detail",
            "brand-list",
            "brand-detail",
            "product-list",
            "product-detail",
            "variant-list",
            "variant-detail",
        )
    },
    "variant-price": {"owner", "branch_scoped", "write"},
    "variant-price-history": {"authenticated", "branch_scoped", "read"},
    "variant-current-price-view": {"authenticated", "branch_scoped", "read"},
    # GET: owner or a branch employee downloads the image bytes (streamed via
    # storage). DELETE: owner clears it. Cross-branch / unknown id -> 404.
    "product-image": {"authenticated", "read", "owner", "write", "branch_scoped"},
    # --- inventory (owner only) ------------------------------------- #
    **{
        name: {"owner", "branch_scoped", "read", "write"}
        for name in (
            "supplier-list",
            "supplier-detail",
            "restock-list",
            "restock-detail",
            "stock-count-list",
            "stock-count-detail",
        )
    },
    "restock-confirm": {"owner", "branch_scoped", "write"},
    "stock-count-submit": {"owner", "branch_scoped", "write"},
    "stock-count-apply": {"owner", "branch_scoped", "write"},
    # Stock balances + low-stock are visible to any branch user; the serializer
    # strips cost for non-owners (InventoryBalanceEmployeeSerializer).
    "inventory-list": {"authenticated", "branch_scoped", "read"},
    "inventory-low-stock": {"authenticated", "branch_scoped", "read"},
    "inventory-adjustments": {"owner", "branch_scoped", "write"},
    "inventory-movements": {"owner", "branch_scoped", "read"},
    "inventory-opening": {"owner", "branch_scoped", "write"},
    "inventory-stock-value": {"owner", "branch_scoped", "read"},
    # --- sales (authenticated + MFA; branch-scoped; cashier sees own) - #
    "sale-list": {"authenticated", "mfa", "branch_scoped", "read"},
    "sale-detail": {"authenticated", "mfa", "branch_scoped", "read"},
    "sale-drafts": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-draft-cart": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-discount-requests": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-finalise": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-cancel": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-return-requests": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:sales_write",
    },
    "sale-receipt": {"authenticated", "mfa", "branch_scoped", "read"},
    "sale-receipt-pdf": {"authenticated", "mfa", "branch_scoped", "read"},
    "customer-list": {"authenticated", "mfa", "branch_scoped", "read", "write"},
    "customer-detail": {"authenticated", "mfa", "branch_scoped", "read", "write"},
    "approval-list": {"authenticated", "mfa", "branch_scoped", "read"},
    "approval-detail": {"authenticated", "mfa", "branch_scoped", "read"},
    "approval-approve": {"owner", "mfa", "branch_scoped", "write"},
    "approval-reject": {"owner", "mfa", "branch_scoped", "write"},
    "return-list": {"authenticated", "mfa", "branch_scoped", "read"},
    "return-detail": {"authenticated", "mfa", "branch_scoped", "read"},
    # --- finance (owner only) ------------------------------------------ #
    "expense-category-list": {"owner", "branch_scoped", "read", "write"},
    "expense-category-detail": {"owner", "branch_scoped", "read", "write"},
    "expense-list": {"owner", "branch_scoped", "read", "write"},
    "expense-detail": {"owner", "branch_scoped", "read", "write"},
    "expense-void": {"owner", "branch_scoped", "write"},
    "report-profit": {"owner", "branch_scoped", "read"},
    "report-best-sellers": {"owner", "branch_scoped", "read"},
    "report-slow-movers": {"owner", "branch_scoped", "read"},
    "report-inventory": {"owner", "branch_scoped", "read"},
    # --- notifications (authenticated + MFA; recipient/user scoped) --- #
    "notification-list": {"authenticated", "mfa", "recipient_scoped", "read"},
    "notification-detail": {"authenticated", "mfa", "recipient_scoped", "read"},
    "notification-read": {"authenticated", "mfa", "recipient_scoped", "write"},
    "notification-read-all": {"authenticated", "mfa", "recipient_scoped", "write"},
    "notification-unread-count": {"authenticated", "mfa", "recipient_scoped", "read"},
    "push-subscription-list": {
        "authenticated",
        "mfa",
        "user_scoped",
        "read",
        "write",
        "throttle:notifications_write",
    },
    "push-subscription-detail": {
        "authenticated",
        "mfa",
        "user_scoped",
        "read",
        "write",
        "throttle:notifications_write",
    },
    "push-subscription-deactivate": {
        "authenticated",
        "mfa",
        "user_scoped",
        "write",
        "throttle:notifications_write",
    },
    "push-subscription-public-key": {"authenticated", "mfa", "read"},
    # --- offline checkout ------------------------------------------- #
    "offline-device-list": {
        "owner",
        "mfa",
        "branch_scoped",
        "read",
        "write",
        "throttle:offline_sync",
    },
    "offline-device-detail": {
        "owner",
        "mfa",
        "branch_scoped",
        "read",
        "write",
        "throttle:offline_sync",
    },
    "offline-device-revoke": {
        "owner",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:offline_sync",
    },
    "offline-device-replace": {
        "owner",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:offline_sync",
    },
    "offline-authorization-list": {
        "authenticated",
        "mfa",
        "owner",
        "branch_scoped",
        "read",
        "write",
        "throttle:offline_sync",
    },
    "offline-authorization-detail": {"authenticated", "mfa", "branch_scoped", "read"},
    "offline-authorization-revoke": {
        "owner",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:offline_sync",
    },
    "offline-authorization-replace": {
        "owner",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:offline_sync",
    },
    "offline-authorization-end-session": {
        "owner",
        "mfa",
        "branch_scoped",
        "write",
        "throttle:offline_sync",
    },
    "offline-authorization-session-status": {
        "authenticated",
        "mfa",
        "branch_scoped",
        "read",
    },
    "offline-sync-record-list": {"owner", "mfa", "branch_scoped", "read"},
    "offline-sync-record-detail": {"owner", "mfa", "branch_scoped", "read"},
    "offline-sync-record-resolve": {"owner", "mfa", "branch_scoped", "write"},
    "offline-sync": {
        "authenticated",
        "mfa",
        "device_authorised",
        "write",
        "throttle:offline_sync",
    },
    "offline-temporary-receipt": {
        "authenticated",
        "mfa",
        "device_authorised",
        "write",
        "throttle:offline_sync",
    },
    "offline-sale-lookup": {"authenticated", "mfa", "branch_scoped", "read"},
}
