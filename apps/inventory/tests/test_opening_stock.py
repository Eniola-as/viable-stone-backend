from decimal import Decimal

import pytest

from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.restock import confirm_restock
from apps.inventory.services.stock import open_stock

from .factories import RestockFactory

pytestmark = pytest.mark.django_db


def _variant(branch):
    return ProductVariantFactory(product=ProductFactory(branch=branch))


def test_opening_stock_sets_quantity_cost_and_single_movement(branch, owner):
    variant = _variant(branch)
    balance = open_stock(
        branch=branch,
        variant=variant,
        quantity=40,
        unit_cost=Decimal("1250.00"),
        created_by=owner,
    )
    assert balance.quantity == 40
    assert balance.average_unit_cost == Decimal("1250.00")
    movements = StockMovement.objects.filter(branch=branch, variant=variant)
    assert movements.count() == 1
    assert movements.first().movement_type == MovementType.OPENING


def test_opening_stock_can_only_happen_once(branch, owner):
    variant = _variant(branch)
    open_stock(
        branch=branch,
        variant=variant,
        quantity=10,
        unit_cost=Decimal("5"),
        created_by=owner,
    )
    with pytest.raises(Conflict):
        open_stock(
            branch=branch,
            variant=variant,
            quantity=5,
            unit_cost=Decimal("5"),
            created_by=owner,
        )


def test_opening_stock_blocked_after_a_restock_movement(branch, owner):
    variant = _variant(branch)
    restock = RestockFactory(branch=branch, created_by=owner)
    from apps.inventory.models import RestockItem

    RestockItem.objects.create(
        restock=restock, variant=variant, quantity=3, unit_cost=Decimal("100")
    )
    confirm_restock(restock=restock, confirmed_by=owner)

    with pytest.raises(Conflict):
        open_stock(
            branch=branch,
            variant=variant,
            quantity=5,
            unit_cost=Decimal("5"),
            created_by=owner,
        )


def test_opening_stock_rejects_nonpositive_values(branch, owner):
    variant = _variant(branch)
    with pytest.raises(APIError):
        open_stock(
            branch=branch,
            variant=variant,
            quantity=0,
            unit_cost=Decimal("5"),
            created_by=owner,
        )
    with pytest.raises(APIError):
        open_stock(
            branch=branch,
            variant=_variant(branch),
            quantity=5,
            unit_cost=Decimal("0"),
            created_by=owner,
        )
    assert not InventoryBalance.objects.filter(branch=branch, quantity__gt=0).exists()
