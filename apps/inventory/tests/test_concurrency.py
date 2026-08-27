"""Concurrency tests — real threads, real PostgreSQL connections, real locks."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import BranchFactory, OwnerFactory
from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.core.exceptions import Conflict
from apps.inventory.models import (
    InventoryBalance,
    Restock,
    RestockItem,
    StockMovement,
)
from apps.inventory.services.restock import confirm_restock

from .factories import RestockFactory

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _confirm(restock_id, owner_id):
    from django.db import connection

    connection.close()
    try:
        confirm_restock(
            restock=Restock.objects.get(pk=restock_id),
            confirmed_by=User.objects.get(pk=owner_id),
        )
        return "ok"
    except Conflict:
        return "conflict"
    finally:
        connection.close()


def test_two_concurrent_restocks_on_one_variant_sum_correctly():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    variant = ProductVariantFactory(product=ProductFactory(branch=branch))

    restock_ids = []
    for _ in range(2):
        restock = RestockFactory(branch=branch, created_by=owner)
        RestockItem.objects.create(
            restock=restock, variant=variant, quantity=50, unit_cost=Decimal("1000.00")
        )
        restock_ids.append(restock.pk)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda rid: _confirm(rid, owner.pk), restock_ids))

    assert results == ["ok", "ok"]
    balance = InventoryBalance.objects.get(branch=branch, variant=variant)
    assert balance.quantity == 100
    assert balance.average_unit_cost == Decimal("1000.00")
    assert StockMovement.objects.filter(branch=branch, variant=variant).count() == 2


def test_confirming_the_same_restock_twice_in_parallel_applies_once():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    variant = ProductVariantFactory(product=ProductFactory(branch=branch))
    restock = RestockFactory(branch=branch, created_by=owner)
    RestockItem.objects.create(
        restock=restock, variant=variant, quantity=30, unit_cost=Decimal("500.00")
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: _confirm(restock.pk, owner.pk), range(2)))

    assert results == ["conflict", "ok"]
    balance = InventoryBalance.objects.get(branch=branch, variant=variant)
    assert balance.quantity == 30
    assert StockMovement.objects.filter(branch=branch, variant=variant).count() == 1
