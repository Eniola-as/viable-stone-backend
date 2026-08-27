"""Stage 15 — Notification / PushSubscription model constraints."""

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import EmployeeFactory
from apps.notifications.models import Notification, NotificationType, PushSubscription
from apps.notifications.tests.factories import (
    NotificationFactory,
    PushSubscriptionFactory,
)

pytestmark = pytest.mark.django_db


class TestNotificationModel:
    def test_defaults_unread(self):
        note = NotificationFactory()
        assert note.is_read is False
        assert note.read_at is None
        assert note.created_at is not None

    def test_mark_read_is_idempotent(self):
        note = NotificationFactory()
        assert note.mark_read() is True
        first_read_at = note.read_at
        assert note.is_read is True
        assert first_read_at is not None
        # second call changes nothing and reports no transition
        assert note.mark_read() is False
        note.refresh_from_db()
        assert note.read_at == first_read_at

    def test_dedupe_key_unique_per_recipient(self):
        note = NotificationFactory(dedupe_key="LOW_STOCK:abc")
        with transaction.atomic(), pytest.raises(IntegrityError):
            Notification.objects.create(
                recipient=note.recipient,
                branch=note.branch,
                notification_type=NotificationType.LOW_STOCK,
                title="dup",
                message="dup",
                dedupe_key="LOW_STOCK:abc",
            )

    def test_same_dedupe_key_allowed_for_different_recipients(self):
        note = NotificationFactory(dedupe_key="LOW_STOCK:abc")
        other = EmployeeFactory(branch=note.branch)
        twin = Notification.objects.create(
            recipient=other,
            branch=note.branch,
            notification_type=NotificationType.LOW_STOCK,
            title="ok",
            message="ok",
            dedupe_key="LOW_STOCK:abc",
        )
        assert twin.pk != note.pk


class TestPushSubscriptionModel:
    def test_unique_per_user_and_endpoint(self):
        sub = PushSubscriptionFactory()
        with transaction.atomic(), pytest.raises(IntegrityError):
            PushSubscription.objects.create(
                user=sub.user,
                endpoint=sub.endpoint,
                p256dh="C" * 87,
                auth="D" * 22,
            )

    def test_same_endpoint_allowed_for_different_users(self):
        sub = PushSubscriptionFactory()
        other = EmployeeFactory(branch=sub.user.branch)
        twin = PushSubscription.objects.create(
            user=other, endpoint=sub.endpoint, p256dh="C" * 87, auth="D" * 22
        )
        assert twin.pk != sub.pk

    def test_deactivate_marks_inactive_and_stamps_expiry(self):
        sub = PushSubscriptionFactory()
        sub.deactivate(expired=True)
        sub.refresh_from_db()
        assert sub.is_active is False
        assert sub.expired_at is not None
