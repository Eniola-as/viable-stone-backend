"""Stage 15 — Web Push sender: no network in tests, failure isolation,
automatic deactivation of dead subscriptions."""

import uuid

import pytest
from django.test import override_settings

from apps.notifications.models import Notification, NotificationType
from apps.notifications.services import dispatch, webpush
from apps.notifications.tests.factories import (
    NotificationFactory,
    PushSubscriptionFactory,
)

pytestmark = pytest.mark.django_db

_PUSH_ON = override_settings(
    PUSH_ENABLED=True,
    VAPID_PRIVATE_KEY="test-private",
    VAPID_PUBLIC_KEY="test-public",
    VAPID_ADMIN_EMAIL="ops@viable-stone.test",
)


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class TestDeliver:
    def test_disabled_push_makes_no_call(self, monkeypatch):
        note = NotificationFactory()
        sub = PushSubscriptionFactory(user=note.recipient)
        called = []
        monkeypatch.setattr(webpush, "webpush", lambda **kw: called.append(kw))
        assert webpush.deliver(sub, note) is False
        assert called == []  # PUSH_ENABLED is False in the test settings

    @_PUSH_ON
    def test_success_stamps_last_used_and_clears_failures(self, monkeypatch):
        note = NotificationFactory()
        sub = PushSubscriptionFactory(user=note.recipient, failure_count=4)
        monkeypatch.setattr(webpush, "webpush", lambda **kw: None)
        assert webpush.deliver(sub, note) is True
        sub.refresh_from_db()
        assert sub.failure_count == 0
        assert sub.last_used_at is not None

    @_PUSH_ON
    def test_permanent_rejection_deactivates_subscription(self, monkeypatch):
        note = NotificationFactory()
        sub = PushSubscriptionFactory(user=note.recipient)

        def _boom(**kw):
            raise webpush.WebPushException("gone", response=_Resp(410))

        monkeypatch.setattr(webpush, "webpush", _boom)
        assert webpush.deliver(sub, note) is False
        sub.refresh_from_db()
        assert sub.is_active is False
        assert sub.expired_at is not None

    @_PUSH_ON
    def test_transient_failures_deactivate_only_after_threshold(self, monkeypatch):
        note = NotificationFactory()
        sub = PushSubscriptionFactory(user=note.recipient, failure_count=3)

        def _boom(**kw):
            raise webpush.WebPushException("temporary", response=_Resp(500))

        monkeypatch.setattr(webpush, "webpush", _boom)
        webpush.deliver(sub, note)
        sub.refresh_from_db()
        assert sub.failure_count == 4
        assert sub.is_active is True
        webpush.deliver(sub, note)
        sub.refresh_from_db()
        assert sub.is_active is False


class TestFailureIsolation:
    @_PUSH_ON
    def test_push_error_does_not_break_notification_creation(
        self, monkeypatch, branch, owner, django_capture_on_commit_callbacks
    ):
        PushSubscriptionFactory(user=owner)

        def _explode(**kw):
            raise RuntimeError("push service unreachable")

        monkeypatch.setattr(webpush, "webpush", _explode)

        with django_capture_on_commit_callbacks(execute=True):
            dispatch.enqueue(
                recipients=[owner],
                branch=branch,
                notification_type=NotificationType.LOW_STOCK,
                title="Low stock: SKU-1",
                message="An item is low.",
                dedupe_key=f"LOW_STOCK:{uuid.uuid4()}",
            )

        # the durable record still exists; the push error was swallowed
        assert Notification.objects.filter(recipient=owner).count() == 1
