from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, serializers, viewsets

from apps.core.models import AuditLog
from apps.core.permissions import IsOwnerOrTechAdmin
from apps.core.selectors import audit_logs_for_user


class AuditLogSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(
        source="actor.username", read_only=True, default=None
    )

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "action",
            "target_type",
            "target_id",
            "actor",
            "actor_username",
            "branch",
            "request_id",
            "before",
            "after",
            "created_at",
        ]
        read_only_fields = fields


@extend_schema_view(
    list=extend_schema(summary="List audit history", tags=["Audit"]),
    retrieve=extend_schema(summary="Retrieve one audit entry", tags=["Audit"]),
)
class AuditLogViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = AuditLogSerializer
    permission_classes = [IsOwnerOrTechAdmin]
    search_fields = ["action", "target_type"]
    ordering_fields = ["created_at", "action"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return audit_logs_for_user(self.request.user)
