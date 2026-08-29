from decimal import Decimal

import pytest

from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import (
    InventoryBalance,
    MovementType,
    RestockItem,
    RestockStatus,
    StockMovement,
)
from apps.inventory.services.restock import confirm_restock, restock_purchase_total

from .factories import RestockFactory, SupplierFactory

pytestmark = pytest.mark.django_db


class TestRestockPurchaseTotal:
    """The purchase total is the exact Decimal sum of quantity * unit_cost."""

    def test_one_item(self, branch, owner):
        restock, _ = _restock_with_items(branch, owner, [(13, "1200.00")])
        assert restock_purchase_total(restock) == Decimal("15600.00")

    def test_multiple_items(self, branch, owner):
        restock, _ = _restock_with_items(
            branch, owner, [(13, "1200.00"), (4, "250.00"), (1, "0.99")]
        )
        assert restock_purchase_total(restock) == Decimal("16600.99")

    def test_exact_decimal_arithmetic(self, branch, owner):
        restock, _ = _restock_with_items(branch, owner, [(7, "1200.33"), (3, "0.01")])
        assert restock_purchase_total(restock) == Decimal("8402.34")

    def test_empty_restock_total_is_zero(self, branch, owner):
        restock = RestockFactory(branch=branch, created_by=owner)
        assert restock_purchase_total(restock) == Decimal("0.00")

    def test_tracks_item_add_and_remove(self, branch, owner):
        restock, items = _restock_with_items(
            branch, owner, [(13, "1200.00"), (4, "250.00")]
        )
        assert restock_purchase_total(restock) == Decimal("16600.00")
        items[1].delete()
        assert restock_purchase_total(restock) == Decimal("15600.00")
        RestockItem.objects.create(
            restock=restock,
            variant=ProductVariantFactory(product=items[0].variant.product),
            quantity=2,
            unit_cost=Decimal("500.00"),
        )
        assert restock_purchase_total(restock) == Decimal("16600.00")

    def test_draft_total_is_stored_and_matches_confirmed(self, branch, owner):
        restock, _ = _restock_with_items(
            branch, owner, [(13, "1200.00"), (4, "250.00")]
        )
        # a draft built through the service helper carries the real total
        restock.total_cost = restock_purchase_total(restock)
        restock.save(update_fields=["total_cost", "updated_at"])
        assert restock.total_cost == Decimal("16600.00")
        assert StockMovement.objects.filter(branch=branch).count() == 0  # still draft

        confirm_restock(restock=restock, confirmed_by=owner)
        restock.refresh_from_db()
        assert restock.total_cost == Decimal("16600.00")  # unchanged by confirm


def _restock_with_items(branch, owner, lines):
    supplier = SupplierFactory(branch=branch)
    restock = RestockFactory(branch=branch, supplier=supplier, created_by=owner)
    product = ProductFactory(branch=branch)
    items = []
    for qty, cost in lines:
        variant = ProductVariantFactory(product=product)
        items.append(
            RestockItem.objects.create(
                restock=restock,
                variant=variant,
                quantity=qty,
                unit_cost=Decimal(cost),
            )
        )
    return restock, items


class TestConfirmRestock:
    def test_confirm_applies_quantity_cost_and_one_movement_each(self, branch, owner):
        restock, items = _restock_with_items(
            branch, owner, [(100, "1500.00"), (20, "800.00")]
        )
        confirm_restock(restock=restock, confirmed_by=owner, request=None)

        for item in items:
            balance = InventoryBalance.objects.get(branch=branch, variant=item.variant)
            assert balance.quantity == item.quantity
            assert balance.average_unit_cost == item.unit_cost
            movements = StockMovement.objects.filter(
                branch=branch, variant=item.variant
            )
            assert movements.count() == 1
            assert movements.first().movement_type == MovementType.RESTOCK
            assert movements.first().quantity_delta == item.quantity

        restock.refresh_from_db()
        assert restock.status == RestockStatus.CONFIRMED
        assert restock.confirmed_by_id == owner.id
        # 100 * 1500.00 + 20 * 800.00
        assert restock.total_cost == Decimal("166000.00")

    def test_weighted_average_after_second_restock(self, branch, owner):
        variant = ProductVariantFactory(product=ProductFactory(branch=branch))

        first = RestockFactory(branch=branch, created_by=owner)
        RestockItem.objects.create(
            restock=first, variant=variant, quantity=100, unit_cost=Decimal("1500.00")
        )
        confirm_restock(restock=first, confirmed_by=owner)

        second = RestockFactory(branch=branch, created_by=owner)
        RestockItem.objects.create(
            restock=second, variant=variant, quantity=50, unit_cost=Decimal("1800.00")
        )
        confirm_restock(restock=second, confirmed_by=owner)

        balance = InventoryBalance.objects.get(branch=branch, variant=variant)
        assert balance.quantity == 150
        assert balance.average_unit_cost == Decimal("1600.00")
        assert StockMovement.objects.filter(branch=branch, variant=variant).count() == 2

    def test_cannot_confirm_twice(self, branch, owner):
        restock, _ = _restock_with_items(branch, owner, [(10, "100.00")])
        confirm_restock(restock=restock, confirmed_by=owner)
        with pytest.raises(Conflict):
            confirm_restock(restock=restock, confirmed_by=owner)
        # Still exactly one movement — the second attempt applied nothing.
        assert StockMovement.objects.filter(branch=branch).count() == 1

    def test_empty_restock_rejected(self, branch, owner):
        restock = RestockFactory(branch=branch, created_by=owner)
        with pytest.raises(APIError):
            confirm_restock(restock=restock, confirmed_by=owner)

    def test_failed_item_rolls_back_whole_restock(self, branch, owner, monkeypatch):
        restock, _ = _restock_with_items(
            branch, owner, [(10, "100.00"), (5, "200.00"), (7, "300.00")]
        )
        import apps.inventory.services.restock as restock_mod

        real_write = restock_mod.write_movement
        calls = {"n": 0}

        def flaky_write(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom on second item")
            return real_write(**kwargs)

        monkeypatch.setattr(restock_mod, "write_movement", flaky_write)

        with pytest.raises(RuntimeError):
            confirm_restock(restock=restock, confirmed_by=owner)

        restock.refresh_from_db()
        assert restock.status == RestockStatus.DRAFT
        assert StockMovement.objects.filter(branch=branch).count() == 0
        assert (
            InventoryBalance.objects.filter(branch=branch, quantity__gt=0).count() == 0
        )
