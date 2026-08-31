"""Real-thread PostgreSQL concurrency for offline-sale reconciliation.

Two owners hitting the same held record must not both win: at most one Sale,
one receipt, one payment set, one stock deduction, one reconciliation row;
refund and sale reconciliations are mutually exclusive; a retry reuses the
result.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
    RegisteredDeviceFactory,
)
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import (
    OfflineSaleReconciliation,
    OfflineSaleSyncRecord,
    Payment,
    Sale,
)
from apps.sales.services.offline import (
    OfflinePaymentInput,
    OfflineSaleInput,
    issue_offline_authorization,
    sync_offline_batch,
)
from apps.sales.services.offline_reconciliation import reconcile_sync_record
from apps.sales.services.offline_snapshot import verify_authorization_token
from apps.sales.tests.factories import stocked_variant

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _held_conflict(stock=5, sale_qty=3):
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(
        branch,
        owner,
        price="1000.00",
        quantity=stock,
        unit_cost="600.00",
        low_stock_level=1,
    )
    auth = issue_offline_authorization(
        branch=branch,
        device=RegisteredDeviceFactory(branch=branch),
        cashier=cashier,
        owner=owner,
    )
    snapshot = verify_authorization_token(auth.signed_token)["snapshot"]
    good, bad = uuid.uuid4(), uuid.uuid4()

    def _mk(cid, seq):
        return OfflineSaleInput(
            client_sale_id=cid,
            device_sequence=seq,
            offline_created_at=timezone.now(),
            items=[{"variant_id": str(variant.id), "quantity": sale_qty}],
            payments=[
                OfflinePaymentInput(method="CASH", amount=Decimal(str(1000 * sale_qty)))
            ],
        )

    sync_offline_batch(
        branch=branch,
        authorization=auth,
        snapshot=snapshot,
        sales=[_mk(good, 1), _mk(bad, 2)],
    )
    rec = OfflineSaleSyncRecord.objects.get(branch=branch, client_sale_id=bad)
    assert rec.outcome == "CONFLICT"
    return branch, owner, variant, rec


def _recorded_as_sale(rec_pk, owner_pk, variant_pk, counted):
    connection.close()
    try:
        rec = OfflineSaleSyncRecord.objects.get(pk=rec_pk)
        from apps.accounts.models import User

        owner = User.objects.get(pk=owner_pk)
        recon = reconcile_sync_record(
            record=rec,
            owner=owner,
            kind="RECORDED_AS_SALE",
            explanation="counted the shelf, the customer kept the goods",
            counts=[{"variant": str(variant_pk), "counted_on_hand": counted}],
        )
        return ("ok", str(recon.kind), str(recon.sale_id))
    except Exception as exc:
        return ("err", getattr(exc, "code", type(exc).__name__), "")
    finally:
        connection.close()


def _refunded(rec_pk, owner_pk, amount):
    connection.close()
    try:
        rec = OfflineSaleSyncRecord.objects.get(pk=rec_pk)
        from apps.accounts.models import User

        owner = User.objects.get(pk=owner_pk)
        recon = reconcile_sync_record(
            record=rec,
            owner=owner,
            kind="REFUNDED_AND_RETURNED",
            explanation="customer returned everything and was refunded in full",
            all_goods_returned=True,
            full_amount_refunded=True,
            refunds=[{"method": "CASH", "amount": amount, "reference": ""}],
        )
        return ("ok", str(recon.kind), "")
    except Exception as exc:
        return ("err", getattr(exc, "code", type(exc).__name__), "")
    finally:
        connection.close()


def test_concurrent_recorded_as_sale_creates_exactly_one_sale():
    branch, owner, variant, rec = _held_conflict(stock=5, sale_qty=3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _recorded_as_sale(rec.pk, owner.pk, variant.pk, 0),
                range(2),
            )
        )

    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 2  # both return the same reconciliation (idempotent)
    assert len({r[2] for r in oks}) == 1  # same sale id

    assert (
        Sale.objects.filter(branch=branch, client_sale_id=rec.client_sale_id).count()
        == 1
    )
    sale = Sale.objects.get(branch=branch, client_sale_id=rec.client_sale_id)
    assert sale.receipt_number
    assert Payment.objects.filter(sale=sale).count() == 1
    assert OfflineSaleReconciliation.objects.filter(sync_record=rec).count() == 1
    assert (
        StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.SALE, reference_id=sale.id
        ).count()
        == 1
    )
    bal = InventoryBalance.objects.get(branch=branch, variant=variant)
    assert bal.quantity == 0  # counted_on_hand
    assert bal.quantity >= 0


def test_concurrent_refund_versus_sale_has_one_winner():
    _branch, owner, variant, rec = _held_conflict(stock=5, sale_qty=3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_sale = pool.submit(_recorded_as_sale, rec.pk, owner.pk, variant.pk, 0)
        f_refund = pool.submit(_refunded, rec.pk, owner.pk, "3000.00")
        results = [f_sale.result(), f_refund.result()]

    kinds_ok = sorted(r[1] for r in results if r[0] == "ok")
    errs = [r[1] for r in results if r[0] == "err"]
    assert len(kinds_ok) == 1
    assert errs == ["offline_record_already_resolved"]

    assert OfflineSaleReconciliation.objects.filter(sync_record=rec).count() == 1
    recon = OfflineSaleReconciliation.objects.get(sync_record=rec)
    if recon.kind == "RECORDED_AS_SALE":
        assert Sale.objects.filter(client_sale_id=rec.client_sale_id).count() == 1
    else:
        assert not Sale.objects.filter(client_sale_id=rec.client_sale_id).exists()


def test_retry_returns_the_same_official_sale():
    _branch, owner, variant, rec = _held_conflict(stock=5, sale_qty=3)
    a = _recorded_as_sale(rec.pk, owner.pk, variant.pk, 1)
    b = _recorded_as_sale(rec.pk, owner.pk, variant.pk, 1)
    assert a[0] == b[0] == "ok"
    assert a[2] == b[2]
    assert Sale.objects.filter(client_sale_id=rec.client_sale_id).count() == 1
    assert OfflineSaleReconciliation.objects.filter(sync_record=rec).count() == 1
