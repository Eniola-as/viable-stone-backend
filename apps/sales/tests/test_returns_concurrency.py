"""Real-thread concurrency for return approval and stock adjustments."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from apps.accounts.models import Branch, User
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.catalog.models import ProductVariant
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.adjustments import adjust_stock
from apps.sales.models import ApprovalRequest, Refund, SaleReturn
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    RequestLine,
    approve_return,
    submit_return_request,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _completed_sale(branch, owner, *, qty=5, price="1000.00", cost="600.00"):
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(
        branch, owner, price=price, quantity=qty + 10, unit_cost=cost
    )
    sale = create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=qty)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal(price) * qty,
                tendered_amount=Decimal(price) * qty,
            )
        ],
        client_sale_id=uuid.uuid4(),
    )
    return sale, variant, sale.items.get()


def _approve(approval_id, owner_id, item_id, qty, key):
    from django.db import connection

    connection.close()
    try:
        approve_return(
            approval=ApprovalRequest.objects.get(pk=approval_id),
            owner=User.objects.get(pk=owner_id),
            lines=[
                ApprovedLine(sale_item_id=item_id, quantity=qty, condition="RESELLABLE")
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal(1000) * qty)],
            client_return_id=key,
        )
        return "ok"
    except (Conflict, APIError) as exc:
        return getattr(exc, "code", "err")
    finally:
        connection.close()


def test_two_parallel_approvals_of_one_request_apply_once():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    sale, variant, item = _completed_sale(branch, owner, qty=5)
    key = uuid.uuid4()
    req = submit_return_request(
        sale=sale,
        requested_by=owner,
        reason="concurrency",
        lines=[RequestLine(sale_item_id=item.id, quantity=2)],
        client_return_id=key,
    )
    stock_before = InventoryBalance.objects.get(branch=branch, variant=variant).quantity

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: _approve(req.pk, owner.pk, item.id, 2, key), range(2))
        )

    assert results.count("ok") >= 1
    assert SaleReturn.objects.count() == 1
    assert Refund.objects.count() == 1
    assert (
        StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.RETURN
        ).count()
        == 1
    )
    assert (
        InventoryBalance.objects.get(branch=branch, variant=variant).quantity
        == stock_before + 2
    )


def _adjust(branch_id, owner_id, variant_id, key):
    from django.db import connection

    connection.close()
    try:
        adjust_stock(
            branch=Branch.objects.get(pk=branch_id),
            variant=ProductVariant.objects.get(pk=variant_id),
            direction="INCREASE",
            quantity=5,
            reason="Parallel duplicate adjustment guard test case",
            actor=User.objects.get(pk=owner_id),
            client_adjustment_id=key,
        )
        return "ok"
    finally:
        connection.close()


def test_duplicate_adjustment_key_in_parallel_applies_once():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    _sale, variant, _item = _completed_sale(branch, owner, qty=5)
    key = uuid.uuid4()
    start = InventoryBalance.objects.get(branch=branch, variant=variant).quantity

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(lambda _: _adjust(branch.id, owner.id, variant.id, key), range(2))
        )

    assert (
        StockMovement.objects.filter(
            variant=variant,
            movement_type=MovementType.ADJUSTMENT,
            reference_id=key,
        ).count()
        == 1
    )
    assert (
        InventoryBalance.objects.get(branch=branch, variant=variant).quantity
        == start + 5
    )
