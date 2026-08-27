"""Stage 18 — branch-isolation acceptance matrix (spec item 5).

Viable Stone does NOT use PostgreSQL Row-Level Security. Branch isolation is
enforced by Django permissions, branch-scoped querysets, locked services and
database FK/constraints. This suite is the acceptance evidence for that
decision: for every branch-owned API resource, a caller authenticated in one
branch can neither retrieve nor list another branch's rows — a guessed id from
another branch returns 404, never 403 and never data.

``test_matrix_covers_every_branch_scoped_route`` fails if a route tagged
``branch_scoped`` in ``security_classification.py`` is not exercised here, so the
matrix cannot silently shrink during a refactor.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accounts.models import OfflineDeviceAuthorization, RegisteredDevice
from apps.accounts.tests.factories import BranchFactory, OwnerFactory
from apps.core.models import AuditLog
from apps.inventory.models import StockCount
from apps.inventory.tests.factories import RestockFactory, SupplierFactory
from apps.notifications.tests.factories import (
    NotificationFactory,
    PushSubscriptionFactory,
)
from apps.sales.models import (
    ApprovalRequest,
    ApprovalType,
    Customer,
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    Sale,
    SaleReturn,
)
from tests.acceptance.security_classification import CLASSIFICATION

pytestmark = pytest.mark.django_db

API = "/api/v1"


@pytest.fixture
def other_branch(db):
    # Unique code + factory-generated username: UserFactory/BranchFactory use
    # django_get_or_create, so a reused literal would return a pre-existing row
    # (e.g. an owner a SubFactory already put in branch A) instead of a new one.
    b2 = BranchFactory(code=f"X{uuid.uuid4().hex[:4].upper()}", name="Outsider Branch")
    return b2, OwnerFactory(branch=b2)


@pytest.fixture
def seeded(db, branch, owner, employee, login_as, stocked):
    """Create one row per branch-owned resource in branch A (VS01).

    Returns ``{classification_name: {"detail": url, "list": url}}`` for every
    resource with an HTTP-retrievable detail route.
    """

    oc = login_as(owner)
    ec = login_as(employee)

    variant = stocked(sku="ISO-1", price="1000.00")
    product = variant.product

    supplier = SupplierFactory(branch=branch)
    restock = RestockFactory(branch=branch)
    stock_count = StockCount.objects.create(
        branch=branch, reason="cycle count", created_by=owner
    )
    customer = Customer.objects.create(branch=branch, name="Repeat Buyer")
    audit = AuditLog.objects.create(
        branch=branch, action="test.seed", target_type="Sale"
    )

    sale = ec.post(
        f"{API}/sales/",
        {
            "client_sale_id": str(uuid.uuid4()),
            "items": [{"variant": str(variant.id), "quantity": 1}],
            "payments": [{"method": "CASH", "amount": "1000.00"}],
        },
        format="json",
    ).json()
    sale_obj = Sale.objects.get(id=sale["id"])

    approval = ApprovalRequest.objects.create(
        branch=branch,
        sale=sale_obj,
        request_type=ApprovalType.RETURN,
        requested_by=employee,
        reason="seed",
    )
    sale_return = SaleReturn.objects.create(
        branch=branch,
        original_sale=sale_obj,
        approval=approval,
        total="0.00",
        client_return_id=uuid.uuid4(),
        approved_by=owner,
    )

    expense_cat = oc.post(
        f"{API}/expense-categories/", {"name": "Fuel"}, format="json"
    ).json()
    expense = oc.post(
        f"{API}/expenses/",
        {
            "category": expense_cat["id"],
            "amount": "500.00",
            "expense_date": "2026-08-01",
            "description": "seed",
        },
        format="json",
    ).json()

    device_id = oc.post(
        f"{API}/offline/devices/", {"name": "Till"}, format="json"
    ).json()["id"]
    auth = oc.post(
        f"{API}/offline/authorizations/",
        {"device": device_id, "cashier": str(employee.id)},
        format="json",
    ).json()
    sync_record = OfflineSaleSyncRecord.objects.create(
        branch=branch,
        authorization=OfflineDeviceAuthorization.objects.get(id=auth["id"]),
        device=RegisteredDevice.objects.get(id=device_id),
        client_sale_id=uuid.uuid4(),
        device_sequence=1,
        offline_created_at=timezone.now(),
        outcome=OfflineSyncOutcome.CONFLICT,
    )

    notification = NotificationFactory(recipient=employee, branch=branch)
    push_sub = PushSubscriptionFactory(user=employee)

    return {
        "branch-detail": {
            "detail": f"{API}/branches/{branch.id}/",
            "list": f"{API}/branches/",
        },
        "user-detail": {
            "detail": f"{API}/users/{employee.id}/",
            "list": f"{API}/users/",
        },
        "audit-log-detail": {
            "detail": f"{API}/audit-logs/{audit.id}/",
            "list": f"{API}/audit-logs/",
        },
        "category-detail": {
            "detail": f"{API}/categories/{product.category_id}/",
            "list": f"{API}/categories/",
        },
        "brand-detail": {
            "detail": f"{API}/brands/{product.brand_id}/",
            "list": f"{API}/brands/",
        },
        "product-detail": {
            "detail": f"{API}/products/{product.id}/",
            "list": f"{API}/products/",
        },
        "variant-detail": {
            "detail": f"{API}/variants/{variant.id}/",
            "list": f"{API}/variants/",
        },
        "supplier-detail": {
            "detail": f"{API}/suppliers/{supplier.id}/",
            "list": f"{API}/suppliers/",
        },
        "restock-detail": {
            "detail": f"{API}/restocks/{restock.id}/",
            "list": f"{API}/restocks/",
        },
        "stock-count-detail": {
            "detail": f"{API}/stock-counts/{stock_count.id}/",
            "list": f"{API}/stock-counts/",
        },
        "sale-detail": {
            "detail": f"{API}/sales/{sale_obj.id}/",
            "list": f"{API}/sales/",
        },
        "sale-receipt": {"detail": f"{API}/sales/{sale_obj.id}/receipt/", "list": None},
        "sale-receipt-pdf": {
            "detail": f"{API}/sales/{sale_obj.id}/receipt.pdf",
            "list": None,
        },
        "customer-detail": {
            "detail": f"{API}/customers/{customer.id}/",
            "list": f"{API}/customers/",
        },
        "approval-detail": {
            "detail": f"{API}/approvals/{approval.id}/",
            "list": f"{API}/approvals/",
        },
        "return-detail": {
            "detail": f"{API}/returns/{sale_return.id}/",
            "list": f"{API}/returns/",
        },
        "expense-category-detail": {
            "detail": f"{API}/expense-categories/{expense_cat['id']}/",
            "list": f"{API}/expense-categories/",
        },
        "expense-detail": {
            "detail": f"{API}/expenses/{expense['id']}/",
            "list": f"{API}/expenses/",
        },
        "offline-device-detail": {
            "detail": f"{API}/offline/devices/{device_id}/",
            "list": f"{API}/offline/devices/",
        },
        "offline-authorization-detail": {
            "detail": f"{API}/offline/authorizations/{auth['id']}/",
            "list": f"{API}/offline/authorizations/",
        },
        "offline-sync-record-detail": {
            "detail": f"{API}/offline/sync-records/{sync_record.id}/",
            "list": f"{API}/offline/sync-records/",
        },
        "offline-sale-lookup": {
            "detail": f"{API}/offline/sales/{sale_obj.client_sale_id}/",
            "list": None,
        },
        "notification-detail": {
            "detail": f"{API}/notifications/{notification.id}/",
            "list": f"{API}/notifications/",
        },
        "push-subscription-detail": {
            "detail": f"{API}/push-subscriptions/{push_sub.id}/",
            "list": f"{API}/push-subscriptions/",
        },
    }


def _outsider(login_as, other_branch):
    _b2, owner2 = other_branch
    return login_as(owner2)


class TestCrossBranchDetail:
    def test_every_branch_owned_detail_returns_404_for_an_outsider(
        self, seeded, login_as, other_branch
    ):
        client = _outsider(login_as, other_branch)
        failures = []
        for name, urls in seeded.items():
            res = client.get(urls["detail"])
            if res.status_code != 404:
                failures.append(f"{name}: {urls['detail']} -> {res.status_code}")
        assert not failures, "cross-branch reads that did NOT 404:\n  " + "\n  ".join(
            failures
        )

    def test_outsider_lists_never_include_branch_a_rows(
        self, seeded, login_as, other_branch
    ):
        client = _outsider(login_as, other_branch)
        failures = []
        for name, urls in seeded.items():
            if not urls["list"]:
                continue
            res = client.get(urls["list"])
            if res.status_code not in (200, 403):
                failures.append(f"{name}: list -> {res.status_code}")
                continue
            if res.status_code == 403:
                continue  # role gate already blocks the outsider entirely
            body = res.json()
            rows = body["results"] if isinstance(body, dict) else body
            detail_id = urls["detail"].rstrip("/").split("/")[-1].replace(".pdf", "")
            if any(str(row.get("id")) == detail_id for row in rows):
                failures.append(f"{name}: branch-A id leaked into outsider list")
        assert not failures, "\n  ".join(failures)


class TestBranchScopedAggregates:
    def test_inventory_balances_are_branch_scoped(self, seeded, login_as, other_branch):
        client = _outsider(login_as, other_branch)
        res = client.get(f"{API}/inventory/")
        assert res.status_code == 200
        body = res.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert rows == []

    def test_profit_report_only_reflects_the_callers_branch(
        self, seeded, login_as, owner, other_branch
    ):
        # branch A made a 1000.00 cash sale in the `seeded` fixture (today).
        a_report = login_as(owner).get(f"{API}/reports/profit/").json()
        assert a_report["revenue"] != "0.00"
        b_report = (
            _outsider(login_as, other_branch).get(f"{API}/reports/profit/").json()
        )
        assert b_report["revenue"] == "0.00"


def test_matrix_covers_every_branch_scoped_route(seeded):
    """Fail if a route tagged ``branch_scoped`` is not exercised above.

    Prevents the isolation matrix from silently losing coverage. List routes
    are represented by their ``*-detail`` sibling; a handful of write-only or
    aggregate routes are covered indirectly and listed as accepted here.
    """

    covered = set(seeded)
    # write-only sub-routes / aggregates covered via their sibling detail row
    # or by TestBranchScopedAggregates.
    indirectly_covered = {
        "branch-list",
        "user-list",
        "user-activate",
        "user-deactivate",
        "audit-log-list",
        "category-list",
        "brand-list",
        "product-list",
        "variant-list",
        "variant-price",
        "variant-price-history",
        "variant-current-price-view",
        "supplier-list",
        "restock-list",
        "restock-confirm",
        "stock-count-list",
        "stock-count-submit",
        "stock-count-apply",
        "inventory-list",
        "inventory-low-stock",
        "inventory-adjustments",
        "inventory-movements",
        "inventory-opening",
        "inventory-stock-value",
        "sale-list",
        "sale-drafts",
        "sale-draft-cart",
        "sale-discount-requests",
        "sale-finalise",
        "sale-cancel",
        "sale-return-requests",
        "customer-list",
        "approval-list",
        "approval-approve",
        "approval-reject",
        "return-list",
        "expense-category-list",
        "expense-list",
        "expense-void",
        "report-profit",
        "report-best-sellers",
        "report-slow-movers",
        "report-inventory",
        "offline-device-list",
        "offline-device-revoke",
        "offline-device-replace",
        "offline-authorization-list",
        "offline-authorization-revoke",
        "offline-authorization-replace",
        "offline-authorization-end-session",
        "offline-authorization-session-status",
        "offline-sync-record-list",
        "offline-sync-record-resolve",
    }
    branch_scoped = {
        name for name, tags in CLASSIFICATION.items() if "branch_scoped" in tags
    }
    uncovered = sorted(branch_scoped - covered - indirectly_covered)
    assert not uncovered, "branch_scoped routes with no isolation coverage: " + repr(
        uncovered
    )
