"""Stage 15 — real-thread PostgreSQL dedupe: two identical alerts race to one row."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction

from apps.accounts.models import Branch, User
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.notifications.models import Notification, NotificationType
from apps.notifications.services import dispatch

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _fire(owner_id, branch_id, key):
    connection.close()
    try:
        owner = User.objects.get(pk=owner_id)
        branch = Branch.objects.get(pk=branch_id)
        with transaction.atomic():
            dispatch.enqueue(
                recipients=[owner],
                branch=branch,
                notification_type=NotificationType.LOW_STOCK,
                title="Low stock",
                message="An item has fallen to its low-stock level.",
                dedupe_key=key,
            )
        return "ok"
    finally:
        connection.close()


def test_concurrent_identical_alerts_dedupe_to_one_row():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    key = f"LOW_STOCK:{uuid.uuid4()}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _fire(owner.pk, branch.pk, key), range(2)))

    assert results == ["ok", "ok"]
    assert Notification.objects.filter(recipient=owner, dedupe_key=key).count() == 1


def test_two_recipients_each_get_exactly_one_under_concurrency():
    branch = BranchFactory()
    owner = OwnerFactory(branch=branch)
    clerk = EmployeeFactory(branch=branch)
    key = f"APPROVAL_REQUESTED:{uuid.uuid4()}"

    def _fire_multi(_):
        connection.close()
        try:
            people = [User.objects.get(pk=owner.pk), User.objects.get(pk=clerk.pk)]
            with transaction.atomic():
                dispatch.enqueue(
                    recipients=people,
                    branch=Branch.objects.get(pk=branch.pk),
                    notification_type=NotificationType.APPROVAL_REQUESTED,
                    title="Approval requested",
                    message="A request is awaiting a decision.",
                    dedupe_key=key,
                )
            return "ok"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_fire_multi, range(3)))

    assert Notification.objects.filter(dedupe_key=key).count() == 2
    assert Notification.objects.filter(dedupe_key=key, recipient=owner).count() == 1
    assert Notification.objects.filter(dedupe_key=key, recipient=clerk).count() == 1
