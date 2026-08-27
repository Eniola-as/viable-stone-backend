"""Real-thread concurrency for checkout: idempotency and no oversell."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from apps.accounts.models import Branch, User
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.core.exceptions import APIError
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import Sale
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

from .factories import stocked_variant

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _sell(branch_id, cashier_id, variant_id, qty, key, amount):
    from django.db import connection

    connection.close()
    try:
        sale = create_sale(
            branch=Branch.objects.get(pk=branch_id),
            cashier=User.objects.get(pk=cashier_id),
            cart=[CartLine(variant_id=variant_id, quantity=qty)],
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal(amount),
                    tendered_amount=Decimal(amount),
                )
            ],
            client_sale_id=key,
        )
        return ("ok", sale.id)
    except APIError as exc:
        return ("err", exc.code)
    finally:
        connection.close()


def test_same_idempotency_key_in_parallel_creates_one_sale_and_one_movement():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(branch, owner, price="1000.00", quantity=20)
    key = uuid.uuid4()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _sell(branch.id, cashier.id, variant.id, 3, key, "3000.00"),
                range(2),
            )
        )

    outcomes = [r[0] for r in results]
    # Either both return the same completed sale, or one returns "in progress".
    assert "ok" in outcomes
    sale_ids = {r[1] for r in results if r[0] == "ok"}
    assert len(sale_ids) == 1
    assert Sale.objects.count() == 1
    assert (
        StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.SALE
        ).count()
        == 1
    )
    assert InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 17


def test_two_sales_cannot_oversell_the_same_variant():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(branch, owner, price="1000.00", quantity=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _sell(
                    branch.id,
                    cashier.id,
                    variant.id,
                    2,
                    uuid.uuid4(),
                    "2000.00",
                ),
                range(2),
            )
        )

    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["err", "ok"]
    assert [r[1] for r in results if r[0] == "err"] == ["stock_not_available"]
    assert InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 1
    assert Sale.objects.count() == 1
