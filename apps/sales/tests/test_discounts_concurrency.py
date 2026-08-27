"""Real-thread concurrency for discounted-draft finalisation."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import Payment, Sale
from apps.sales.services.discounts import (
    approve_discount,
    create_draft_sale,
    finalise_draft,
    request_discount,
)
from apps.sales.services.sales import CartLine, PaymentLine
from apps.sales.tests.factories import stocked_variant

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _finalise(sale_id, cashier_id):
    from django.db import connection

    connection.close()
    try:
        finalise_draft(
            sale=Sale.objects.get(pk=sale_id),
            cashier=User.objects.get(pk=cashier_id),
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("4000.00"),
                    tendered_amount=Decimal("4000.00"),
                )
            ],
        )
        return "ok"
    except (Conflict, APIError) as exc:
        return getattr(exc, "code", "err")
    finally:
        connection.close()


def test_concurrent_finalisation_creates_one_sale_one_movement_one_payment():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    v1 = stocked_variant(
        branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
    )
    draft = create_draft_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=v1.id, quantity=5)],  # subtotal 5000
        client_sale_id=uuid.uuid4(),
    )
    req = request_discount(
        sale=draft, requested_by=cashier, amount=Decimal("1000.00"), reason="c"
    )
    approve_discount(approval=req, owner=owner, amount=Decimal("1000.00"))
    start = InventoryBalance.objects.get(branch=branch, variant=v1).quantity

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _finalise(draft.pk, cashier.pk), range(2)))

    assert results.count("ok") >= 1
    completed = Sale.objects.filter(pk=draft.pk, status="COMPLETED")
    assert completed.count() == 1
    assert Payment.objects.filter(sale_id=draft.pk).count() == 1
    assert (
        StockMovement.objects.filter(
            variant=v1, movement_type=MovementType.SALE
        ).count()
        == 1
    )
    assert InventoryBalance.objects.get(branch=branch, variant=v1).quantity == start - 5
