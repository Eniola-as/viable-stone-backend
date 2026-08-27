"""Stage 15 — low/out-of-stock alerts fire once, centrally, through every
stock-changing workflow and never repeat while stock stays low."""

import datetime
import uuid
from decimal import Decimal

import pytest

from apps.accounts.tests.factories import EmployeeFactory, OwnerFactory
from apps.inventory.models import MovementType, StockCount
from apps.inventory.services.adjustments import adjust_stock
from apps.inventory.services.restock import confirm_restock
from apps.inventory.services.stock import lock_balances, write_movement
from apps.inventory.services.stock_count import apply_stock_count, submit_stock_count
from apps.inventory.tests.factories import RestockFactory, SupplierFactory
from apps.notifications.models import Notification, NotificationType
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


def _low_stock_notes(branch):
    return Notification.objects.filter(
        branch=branch, notification_type=NotificationType.LOW_STOCK
    )


def _out_notes(branch):
    return Notification.objects.filter(
        branch=branch, notification_type=NotificationType.OUT_OF_STOCK
    )


class TestSaleCrossings:
    def test_sale_crossing_into_low_alerts_active_owners_only(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        disabled_owner = OwnerFactory(branch=branch, is_active=False)
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=3)],  # 8 -> 5 == low
                payments=[PaymentLine(method="CASH", amount=Decimal("3000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        notes = _low_stock_notes(branch)
        assert notes.count() == 1
        assert notes.first().recipient_id == owner.id
        assert not notes.filter(recipient_id=disabled_owner.id).exists()

    def test_no_repeat_while_still_low(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=3)],  # -> 5 low
                payments=[PaymentLine(method="CASH", amount=Decimal("3000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=1)],  # 5 -> 4 low
                payments=[PaymentLine(method="CASH", amount=Decimal("1000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        assert _low_stock_notes(branch).count() == 1

    def test_direct_drop_to_zero_sends_only_out_of_stock(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=8)],  # 8 -> 0
                payments=[PaymentLine(method="CASH", amount=Decimal("8000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        assert _out_notes(branch).count() == 1
        assert _low_stock_notes(branch).count() == 0


class TestOtherWorkflows:
    def test_restock_above_threshold_resets_cycle(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=4)],  # -> 4 low
                payments=[PaymentLine(method="CASH", amount=Decimal("4000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        assert _low_stock_notes(branch).count() == 1

        supplier = SupplierFactory(branch=branch)
        restock = RestockFactory(
            branch=branch,
            supplier=supplier,
            created_by=owner,
            date=datetime.date.today(),
        )
        restock.items.create(variant=variant, quantity=20, unit_cost=Decimal("100.00"))
        with django_capture_on_commit_callbacks(execute=True):
            confirm_restock(restock=restock, confirmed_by=owner)  # 4 -> 24 OK
        assert _low_stock_notes(branch).count() == 1  # no new alert on recovery

        # Falling low again is a fresh crossing -> one more alert.
        with django_capture_on_commit_callbacks(execute=True):
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=20)],  # 24 -> 4 low
                payments=[PaymentLine(method="CASH", amount=Decimal("20000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        assert _low_stock_notes(branch).count() == 2

    def test_protected_adjustment_decrease_triggers_alert(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=9, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=True):
            adjust_stock(
                branch=branch,
                variant=variant,
                direction="DECREASE",
                quantity=5,  # 9 -> 4 low
                reason="Damaged in the store room, verified by owner.",
                actor=owner,
                client_adjustment_id=uuid.uuid4(),
            )
        assert _low_stock_notes(branch).count() == 1

    def test_stock_count_shrinkage_triggers_alert(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=10, low_stock_level=5
        )
        count = StockCount.objects.create(
            branch=branch, reason="Quarterly count", created_by=owner
        )
        count.items.create(
            variant=variant, system_quantity_snapshot=10, counted_quantity=3
        )
        submit_stock_count(stock_count=count, actor=owner)
        with django_capture_on_commit_callbacks(execute=True):
            apply_stock_count(stock_count=count, applied_by=owner)  # 10 -> 3 low
        assert _low_stock_notes(branch).count() == 1

    def test_return_restock_from_zero_to_low_emits_one_low_alert(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=2, low_stock_level=5
        )
        balance = lock_balances(branch, [variant.id])[variant.id]
        # drop straight to zero -> OUT
        with django_capture_on_commit_callbacks(execute=True):
            write_movement(
                balance=balance,
                delta=-2,
                movement_type=MovementType.SALE,
                reference_type="sale",
                reference_id=uuid.uuid4(),
            )
        assert _out_notes(branch).count() == 1
        # a RETURN pushes it to 3 -> still low -> exactly one LOW alert
        balance = lock_balances(branch, [variant.id])[variant.id]
        with django_capture_on_commit_callbacks(execute=True):
            write_movement(
                balance=balance,
                delta=3,
                movement_type=MovementType.RETURN,
                reference_type="sale_return",
                reference_id=uuid.uuid4(),
            )
        assert _low_stock_notes(branch).count() == 1


class TestRollback:
    def test_rolled_back_transaction_produces_no_notification(self, branch, owner):
        from django.db import transaction

        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )

        class Boom(Exception):
            pass

        with pytest.raises(Boom), transaction.atomic():
            balance = lock_balances(branch, [variant.id])[variant.id]
            write_movement(
                balance=balance,
                delta=-4,
                movement_type=MovementType.SALE,
                reference_type="sale",
                reference_id=uuid.uuid4(),
            )
            raise Boom

        assert Notification.objects.count() == 0
