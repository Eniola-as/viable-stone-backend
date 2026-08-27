"""Stage 15 corrections — the durable Notification row lives in the SAME
transaction as the business event; only Web Push is deferred to on_commit."""

import uuid
from decimal import Decimal

import pytest
from django.db import transaction

from apps.accounts.tests.factories import EmployeeFactory
from apps.inventory.models import MovementType, StockMovement
from apps.inventory.services.stock import lock_balances, write_movement
from apps.notifications.models import Notification, NotificationType
from apps.notifications.services import dispatch, webpush
from apps.notifications.tests.factories import NotificationFactory
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

LOW = NotificationType.LOW_STOCK


class TestRowInsideBusinessTransaction:
    def test_row_is_written_synchronously_before_on_commit_runs(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=3)],  # 8 -> 5 low
                payments=[PaymentLine(method="CASH", amount=Decimal("3000.00"))],
                client_sale_id=uuid.uuid4(),
            )
            # the durable row already exists, before any on_commit callback fires
            assert Notification.objects.filter(notification_type=LOW).count() == 1

        # running the deferred callbacks creates nothing new — they are push-only
        for callback in callbacks:
            callback()
        assert Notification.objects.filter(notification_type=LOW).count() == 1

    def test_rollback_removes_both_the_stock_movement_and_the_notification(
        self, branch, owner
    ):
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )

        with pytest.raises(RuntimeError), transaction.atomic():
            balance = lock_balances(branch, [variant.id])[variant.id]
            write_movement(
                balance=balance,
                delta=-4,  # 8 -> 4 low
                movement_type=MovementType.SALE,
                reference_type="sale",
                reference_id=uuid.uuid4(),
            )
            # created in the same transaction, visible immediately
            assert Notification.objects.filter(notification_type=LOW).count() == 1
            raise RuntimeError("business event failed")

        assert not Notification.objects.filter(notification_type=LOW).exists()
        assert not StockMovement.objects.filter(reference_type="sale").exists()

    def test_dedupe_collision_is_absorbed_without_poisoning_the_transaction(
        self, branch, owner
    ):
        key = f"LOW_STOCK:{uuid.uuid4()}"
        with transaction.atomic():
            NotificationFactory(
                recipient=owner,
                branch=branch,
                dedupe_key=key,
                notification_type=LOW,
            )
            # would violate the (recipient, dedupe_key) unique constraint
            dispatch.enqueue(
                recipients=[owner],
                branch=branch,
                notification_type=LOW,
                title="dup",
                message="dup",
                dedupe_key=key,
            )
            # the surrounding transaction is still usable
            marker = NotificationFactory(
                recipient=owner,
                branch=branch,
                dedupe_key=f"{key}:marker",
                notification_type=LOW,
            )

        assert Notification.objects.filter(dedupe_key=key).count() == 1
        assert Notification.objects.filter(pk=marker.pk).exists()


class TestPushIsolation:
    def test_push_error_never_affects_the_committed_sale_or_the_row(
        self, branch, owner, monkeypatch, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=8, low_stock_level=5
        )

        with (
            override_push_enabled(),
            django_capture_on_commit_callbacks(execute=True),
        ):
            monkeypatch.setattr(
                webpush,
                "webpush",
                _raise_runtime,
            )
            sale = create_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=3)],
                payments=[PaymentLine(method="CASH", amount=Decimal("3000.00"))],
                client_sale_id=uuid.uuid4(),
            )

        sale.refresh_from_db()
        assert sale.status == "COMPLETED"
        assert Notification.objects.filter(notification_type=LOW).count() == 1


def _raise_runtime(**_kwargs):
    raise RuntimeError("push service unreachable")


def override_push_enabled():
    from django.test import override_settings

    return override_settings(
        PUSH_ENABLED=True,
        VAPID_PRIVATE_KEY="unit-test-private",
        VAPID_PUBLIC_KEY="unit-test-public",
        VAPID_ADMIN_EMAIL="ops@viable-stone.test",
    )
