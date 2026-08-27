from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsAuthenticatedAndMFAVerified
from apps.notifications.api.serializers import (
    NotificationSerializer,
    PushSubscriptionSerializer,
    PushSubscriptionWriteSerializer,
    ReadAllResultSerializer,
    UnreadCountSerializer,
    VapidPublicKeySerializer,
)
from apps.notifications.models import Notification, PushSubscription


@extend_schema_view(
    list=extend_schema(summary="List my notifications", tags=["Notifications"]),
    retrieve=extend_schema(
        summary="Retrieve one of my notifications", tags=["Notifications"]
    ),
)
class NotificationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """A user sees only their own notifications. Clients cannot create, edit or
    delete them — only list, read and mark-as-read."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticatedAndMFAVerified]
    queryset = Notification.objects.none()
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = Notification.objects.filter(recipient=self.request.user)
        is_read = self.request.query_params.get("is_read")
        if is_read in {"true", "false"}:
            queryset = queryset.filter(is_read=is_read == "true")
        return queryset

    @extend_schema(
        responses={200: UnreadCountSerializer},
        summary="Count my unread notifications",
        tags=["Notifications"],
    )
    @action(detail=False, url_path="unread-count")
    def unread_count(self, request):
        count = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).count()
        return Response({"unread": count})

    @extend_schema(
        request=None,
        responses={200: NotificationSerializer},
        summary="Mark one notification read (idempotent)",
        tags=["Notifications"],
    )
    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()
        notification.mark_read()
        return Response(self.get_serializer(notification).data)

    @extend_schema(
        request=None,
        responses={200: ReadAllResultSerializer},
        summary="Mark all my notifications read (idempotent)",
        tags=["Notifications"],
    )
    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        updated = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True, read_at=timezone.now())
        return Response({"updated": updated})


@extend_schema_view(
    list=extend_schema(summary="List my push subscriptions", tags=["Notifications"]),
    retrieve=extend_schema(
        summary="Retrieve one of my push subscriptions", tags=["Notifications"]
    ),
    destroy=extend_schema(
        summary="Delete one of my push subscriptions", tags=["Notifications"]
    ),
)
class PushSubscriptionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """A user manages only their own browser/device subscriptions. Registration
    upserts on ``(user, endpoint)`` so re-subscribing the same browser is safe.
    Write actions are rate limited."""

    serializer_class = PushSubscriptionSerializer
    permission_classes = [IsAuthenticatedAndMFAVerified]
    queryset = PushSubscription.objects.none()
    ordering = ["-created_at"]

    def get_queryset(self):
        return PushSubscription.objects.filter(user=self.request.user)

    def get_throttles(self):
        if self.action in {"create", "destroy", "deactivate"}:
            self.throttle_scope = "notifications_write"
        return super().get_throttles()

    @extend_schema(
        request=PushSubscriptionWriteSerializer,
        responses={201: PushSubscriptionSerializer, 200: PushSubscriptionSerializer},
        summary="Register or update (upsert) a browser push subscription",
        tags=["Notifications"],
    )
    def create(self, request, *args, **kwargs):
        serializer = PushSubscriptionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        obj, created = PushSubscription.objects.update_or_create(
            user=request.user,
            endpoint=data["endpoint"],
            defaults={
                "p256dh": data["p256dh"],
                "auth": data["auth"],
                "user_agent": data.get("user_agent", ""),
                "is_active": True,
                "expired_at": None,
                "failure_count": 0,
            },
        )
        return Response(
            PushSubscriptionSerializer(obj).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses={200: PushSubscriptionSerializer},
        summary="Deactivate one of my push subscriptions",
        tags=["Notifications"],
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        subscription = self.get_object()
        subscription.deactivate()
        return Response(self.get_serializer(subscription).data)

    @extend_schema(
        responses={200: VapidPublicKeySerializer},
        summary="The browser-safe VAPID public key",
        tags=["Notifications"],
    )
    @action(detail=False, url_path="public-key")
    def public_key(self, request):
        return Response({"public_key": settings.VAPID_PUBLIC_KEY})
