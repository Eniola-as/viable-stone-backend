from rest_framework.routers import DefaultRouter

from apps.core.api.audit_views import AuditLogViewSet

router = DefaultRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
