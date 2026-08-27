import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory
from apps.notifications.models import (
    Notification,
    NotificationType,
    PushSubscription,
)


class NotificationFactory(DjangoModelFactory):
    class Meta:
        model = Notification

    recipient = factory.SubFactory(EmployeeFactory)
    branch = factory.SelfAttribute("recipient.branch")
    notification_type = NotificationType.LOW_STOCK
    title = "Low stock"
    message = "An item has fallen to its low-stock level."
    dedupe_key = factory.Sequence(lambda n: f"evt:{n}")


class PushSubscriptionFactory(DjangoModelFactory):
    class Meta:
        model = PushSubscription

    user = factory.SubFactory(EmployeeFactory)
    endpoint = factory.Sequence(lambda n: f"https://push.example.com/sub/{n}")
    p256dh = "B" * 87
    auth = "A" * 22
    user_agent = "Mozilla/5.0 (Test)"


__all__ = [
    "BranchFactory",
    "NotificationFactory",
    "PushSubscriptionFactory",
]
