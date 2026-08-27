"""Durable in-app notifications and browser push subscriptions.

Every alert is written here as a durable :class:`Notification` row inside the
same transaction as the business event that raised it (delivery rule 1); a
rollback removes both. Only the external browser push is deferred with
``transaction.on_commit`` and is best-effort, so a push failure can never roll
back the sale, stock movement, return, approval or adjustment that triggered it.
Notification and push content must never carry costs, profit, audit details or
private customer information.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel


class NotificationType(models.TextChoices):
    LOW_STOCK = "LOW_STOCK", "Low stock"
    OUT_OF_STOCK = "OUT_OF_STOCK", "Out of stock"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED", "Approval requested"
    APPROVAL_APPROVED = "APPROVAL_APPROVED", "Approval approved"
    APPROVAL_REJECTED = "APPROVAL_REJECTED", "Approval rejected"


class Notification(BaseModel):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="notifications"
    )
    notification_type = models.CharField(
        max_length=20, choices=NotificationType.choices
    )
    title = models.CharField(max_length=140)
    message = models.CharField(max_length=500)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    # A safe pointer to what the alert is about: an opaque type label plus id,
    # never a live relation, so nothing sensitive is reachable through it.
    related_object_type = models.CharField(max_length=40, blank=True)
    related_object_id = models.UUIDField(null=True, blank=True)
    # Frontend deep link, always built from a server-side allowlist.
    action_path = models.CharField(max_length=200, blank=True)
    # One alert per logical event per recipient; the unique constraint makes
    # concurrent or repeated delivery a no-op.
    dedupe_key = models.CharField(max_length=200)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                name="notification_unique_recipient_dedupe",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"]),
            models.Index(fields=["branch", "notification_type"]),
        ]

    def __str__(self):
        return f"{self.notification_type} -> {self.recipient_id}"

    def mark_read(self) -> bool:
        """Mark the notification read. Idempotent.

        Returns ``True`` only on the actual unread -> read transition so callers
        can count how many rows really changed.
        """

        if self.is_read:
            return False
        self.is_read = True
        self.read_at = timezone.now()
        self.save(update_fields=["is_read", "read_at", "updated_at"])
        return True


class PushSubscription(BaseModel):
    """One browser/device Web Push registration for a user.

    A user may hold several. Registration upserts on ``(user, endpoint)``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)
    failure_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "endpoint"], name="pushsub_unique_user_endpoint"
            ),
        ]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return f"push:{self.user_id}:{self.endpoint[:32]}"

    def deactivate(self, *, expired: bool = False) -> None:
        """Turn the subscription off. ``expired`` stamps a one-time expiry time."""

        self.is_active = False
        if expired and self.expired_at is None:
            self.expired_at = timezone.now()
        self.save(update_fields=["is_active", "expired_at", "updated_at"])
