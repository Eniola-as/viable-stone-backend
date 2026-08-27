from decimal import Decimal

import pytest

from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import (
    MovementType,
    StockCount,
    StockCountItem,
    StockCountStatus,
    StockMovement,
)
from apps.inventory.services.stock import open_stock
from apps.inventory.services.stock_count import apply_stock_count, submit_stock_count

pytestmark = pytest.mark.django_db

COUNTS = "/api/v1/stock-counts/"


def _count_with_items(branch, owner, items):
    count = StockCount.objects.create(
        branch=branch, reason="Quarterly count", created_by=owner
    )
    for variant, counted in items:
        StockCountItem.objects.create(
            stock_count=count,
            variant=variant,
            system_quantity_snapshot=0,
            counted_quantity=counted,
            variance=counted,
        )
    return count


class TestApplyStockCount:
    def test_apply_creates_adjustment_movements_for_variances_only(self, branch, owner):
        matches = ProductVariantFactory(product=ProductFactory(branch=branch))
        short = ProductVariantFactory(product=ProductFactory(branch=branch))
        open_stock(
            branch=branch,
            variant=matches,
            quantity=10,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        open_stock(
            branch=branch,
            variant=short,
            quantity=10,
            unit_cost=Decimal("1"),
            created_by=owner,
        )

        count = _count_with_items(branch, owner, [(matches, 10), (short, 7)])
        submit_stock_count(stock_count=count, actor=owner)
        apply_stock_count(stock_count=count, applied_by=owner)

        count.refresh_from_db()
        assert count.status == StockCountStatus.APPLIED
        assert count.applied_by_id == owner.id

        # matched variant: no adjustment movement
        assert not StockMovement.objects.filter(
            variant=matches, movement_type=MovementType.ADJUSTMENT
        ).exists()
        # short variant: one -3 adjustment
        adj = StockMovement.objects.get(
            variant=short, movement_type=MovementType.ADJUSTMENT
        )
        assert adj.quantity_delta == -3
        assert adj.reason == "Quarterly count"
        assert short.balances.get(branch=branch).quantity == 7

        item = count.items.get(variant=short)
        assert item.system_quantity_snapshot == 10
        assert item.variance == -3

    def test_cannot_apply_twice(self, branch, owner):
        variant = ProductVariantFactory(product=ProductFactory(branch=branch))
        open_stock(
            branch=branch,
            variant=variant,
            quantity=5,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        count = _count_with_items(branch, owner, [(variant, 8)])
        submit_stock_count(stock_count=count, actor=owner)
        apply_stock_count(stock_count=count, applied_by=owner)
        with pytest.raises(Conflict):
            apply_stock_count(stock_count=count, applied_by=owner)
        assert (
            StockMovement.objects.filter(
                variant=variant, movement_type=MovementType.ADJUSTMENT
            ).count()
            == 1
        )

    def test_submit_requires_items(self, branch, owner):
        count = StockCount.objects.create(
            branch=branch, reason="empty", created_by=owner
        )
        with pytest.raises(APIError):
            submit_stock_count(stock_count=count, actor=owner)


class TestStockCountApi:
    def test_owner_creates_submits_and_applies_via_api(self, login_as, owner, branch):
        variant = ProductVariantFactory(product__branch=branch)
        open_stock(
            branch=branch,
            variant=variant,
            quantity=4,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        client = login_as(owner)

        created = client.post(
            COUNTS,
            {
                "reason": "spot check",
                "items": [{"variant": str(variant.id), "counted_quantity": 9}],
            },
            format="json",
        )
        assert created.status_code == 201, created.content
        cid = created.json()["id"]
        assert client.post(f"{COUNTS}{cid}/submit/").status_code == 200
        applied = client.post(f"{COUNTS}{cid}/apply/")
        assert applied.status_code == 200
        assert applied.json()["status"] == "APPLIED"
        assert variant.balances.get(branch=branch).quantity == 9

    def test_employee_cannot_access_stock_counts(self, login_as, branch):
        from apps.accounts.tests.factories import EmployeeFactory

        emp = EmployeeFactory(branch=branch)
        assert login_as(emp).get(COUNTS).status_code == 403
