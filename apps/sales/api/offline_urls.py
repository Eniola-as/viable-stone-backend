from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.sales.api.offline_views import (
    OfflineAuthorizationViewSet,
    OfflineDeviceViewSet,
    OfflineSaleLookupView,
    OfflineSyncRecordViewSet,
    OfflineSyncView,
    OfflineTemporaryReceiptView,
)

router = DefaultRouter()
router.register("offline/devices", OfflineDeviceViewSet, basename="offline-device")
router.register(
    "offline/authorizations",
    OfflineAuthorizationViewSet,
    basename="offline-authorization",
)
router.register(
    "offline/sync-records",
    OfflineSyncRecordViewSet,
    basename="offline-sync-record",
)

urlpatterns = [
    path("offline/sync/", OfflineSyncView.as_view(), name="offline-sync"),
    path(
        "offline/temporary-receipt/",
        OfflineTemporaryReceiptView.as_view(),
        name="offline-temporary-receipt",
    ),
    path(
        "offline/sales/<uuid:client_sale_id>/",
        OfflineSaleLookupView.as_view(),
        name="offline-sale-lookup",
    ),
    *router.urls,
]
