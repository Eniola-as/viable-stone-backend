"""Stage 16 — real-thread PostgreSQL concurrency for offline sync."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.accounts.models import OfflineDeviceAuthorization
from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
    RegisteredDeviceFactory,
)
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import OfflineSaleSyncRecord, OfflineSyncOutcome, Payment, Sale
from apps.sales.services.offline import (
    OfflinePaymentInput,
    OfflineSaleInput,
    issue_offline_authorization,
    sync_offline_batch,
)
from apps.sales.services.offline_snapshot import verify_authorization_token
from apps.sales.tests.factories import stocked_variant

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _cash(amount):
    return [OfflinePaymentInput(method="CASH", amount=Decimal(amount))]


def _sale(vid, *, seq, qty, cid=None):
    return OfflineSaleInput(
        client_sale_id=cid or uuid.uuid4(),
        device_sequence=seq,
        offline_created_at=timezone.now(),
        items=[{"variant_id": str(vid), "quantity": qty}],
        payments=_cash(str(1000 * qty)),
    )


def _run(auth_id, sales):
    connection.close()
    try:
        auth = OfflineDeviceAuthorization.objects.get(pk=auth_id)
        snapshot = verify_authorization_token(auth.signed_token)["snapshot"]
        results = sync_offline_batch(
            branch=auth.branch, authorization=auth, snapshot=snapshot, sales=sales
        )
        return [r.outcome for r in results]
    finally:
        connection.close()


def _setup(qty):
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(
        branch, owner, price="1000.00", quantity=qty, low_stock_level=1
    )
    auth = issue_offline_authorization(
        branch=branch,
        device=RegisteredDeviceFactory(branch=branch),
        cashier=cashier,
        owner=owner,
    )
    return branch, variant, auth


def test_concurrent_identical_batches_create_exactly_one_sale():
    branch, variant, auth = _setup(20)
    cid = uuid.uuid4()
    batch = [_sale(variant.id, seq=1, qty=2, cid=cid)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _run(auth.pk, batch), range(2)))

    flat = [o for pair in outcomes for o in pair]
    assert flat.count(OfflineSyncOutcome.ACCEPTED) >= 1
    assert set(flat) <= {OfflineSyncOutcome.ACCEPTED, OfflineSyncOutcome.DUPLICATE}
    assert Sale.objects.filter(branch=branch, client_sale_id=cid).count() == 1
    assert (
        OfflineSaleSyncRecord.objects.filter(branch=branch, client_sale_id=cid).count()
        == 1
    )
    assert Payment.objects.filter(sale__client_sale_id=cid).count() == 1
    assert InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 18


def test_competing_batches_for_scarce_stock_never_oversell():
    branch, variant, auth = _setup(3)
    a = [_sale(variant.id, seq=1, qty=2)]
    b = [_sale(variant.id, seq=2, qty=2)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run, auth.pk, a), pool.submit(_run, auth.pk, b)]
        outcomes = [f.result() for f in futures]

    flat = [o for pair in outcomes for o in pair]
    assert flat.count(OfflineSyncOutcome.ACCEPTED) == 1
    assert flat.count(OfflineSyncOutcome.CONFLICT) == 1

    balance = InventoryBalance.objects.get(branch=branch, variant=variant)
    assert balance.quantity == 1  # only the accepted sale applied
    assert (
        StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.SALE
        ).count()
        == 1
    )
    # the conflicting record is retained, unresolved
    conflict = OfflineSaleSyncRecord.objects.get(
        branch=branch, outcome=OfflineSyncOutcome.CONFLICT
    )
    assert conflict.resolved is False
    assert conflict.sale_id is None
