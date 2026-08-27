from rest_framework.routers import DefaultRouter

from apps.notifications.api.views import (
    NotificationViewSet,
    PushSubscriptionViewSet,
)

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")
router.register(
    "push-subscriptions", PushSubscriptionViewSet, basename="push-subscription"
)

urlpatterns = router.urls
