from urllib.parse import urlparse

from rest_framework import serializers

from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)
from apps.notifications.models import Notification, PushSubscription

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class NotificationSerializer(ControlCharSafeModelSerializer):
    """Read-only view of a notification. Carries no cost, profit, audit or
    private customer data — only the safe title/message and a deep-link path."""

    class Meta:
        model = Notification
        fields = [
            "id",
            "notification_type",
            "title",
            "message",
            "is_read",
            "read_at",
            "created_at",
            "action_path",
            "related_object_type",
            "related_object_id",
        ]
        read_only_fields = fields


class UnreadCountSerializer(ControlCharSafeSerializer):
    unread = serializers.IntegerField()


class ReadAllResultSerializer(ControlCharSafeSerializer):
    updated = serializers.IntegerField()


class PushSubscriptionSerializer(ControlCharSafeModelSerializer):
    """Read view — the p256dh / auth secrets are never echoed back."""

    class Meta:
        model = PushSubscription
        fields = [
            "id",
            "endpoint",
            "user_agent",
            "is_active",
            "created_at",
            "last_used_at",
            "expired_at",
        ]
        read_only_fields = fields


class PushSubscriptionWriteSerializer(ControlCharSafeSerializer):
    endpoint = serializers.URLField(max_length=500)
    p256dh = serializers.CharField(min_length=80, max_length=200)
    auth = serializers.CharField(min_length=16, max_length=100)
    user_agent = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=300
    )

    def validate_endpoint(self, value):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https":
            return value
        if parsed.scheme == "http" and host in _LOCAL_HOSTS:
            return value
        raise serializers.ValidationError(
            "Push endpoints must use HTTPS (plain http is allowed only for "
            "localhost during development)."
        )


class VapidPublicKeySerializer(ControlCharSafeSerializer):
    public_key = serializers.CharField()
