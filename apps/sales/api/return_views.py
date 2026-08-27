from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsAuthenticatedAndMFAVerified, IsOwner
from apps.sales.api.return_serializers import (
    ApprovalRequestSerializer,
    ApproveReturnSerializer,
    RejectReturnSerializer,
    SaleReturnReadSerializer,
)
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    SaleReturn,
)
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    approve_return,
    reject_return,
)


@extend_schema_view(
    list=extend_schema(summary="List approval requests", tags=["Approvals"]),
    retrieve=extend_schema(summary="Retrieve an approval request", tags=["Approvals"]),
)
class ApprovalViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ApprovalRequestSerializer
    permission_classes = [IsAuthenticatedAndMFAVerified]
    queryset = ApprovalRequest.objects.select_related(
        "requested_by", "reviewed_by", "sale"
    )
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False) or user.branch_id is None:
            return self.queryset.none()
        qs = self.queryset.filter(branch_id=user.branch_id)
        if not getattr(user, "is_owner", False):
            qs = qs.filter(requested_by_id=user.id)
        return qs

    def _pending_return(self):
        approval = self.get_object()
        if approval.request_type != ApprovalType.RETURN:
            from apps.core.exceptions import APIError

            raise APIError(
                "This endpoint only handles return approvals.",
                code="not_a_return",
            )
        return approval

    @extend_schema(
        request=ApproveReturnSerializer,
        responses={200: SaleReturnReadSerializer},
        summary="Approve a return request (owner)",
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], permission_classes=[IsOwner])
    def approve(self, request, pk=None):
        approval = self._pending_return()
        serializer = ApproveReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        sale_return = approve_return(
            approval=approval,
            owner=request.user,
            lines=[
                ApprovedLine(
                    sale_item_id=line["sale_item"],
                    quantity=line["quantity"],
                    condition=line["condition"],
                )
                for line in data["lines"]
            ],
            refunds=[
                RefundLine(
                    method=r["method"],
                    amount=r["amount"],
                    reference=r.get("reference", ""),
                )
                for r in data["refunds"]
            ],
            reviewer_note=data.get("reviewer_note", ""),
            client_return_id=data.get("client_return_id"),
            request=request,
        )
        return Response(
            SaleReturnReadSerializer(sale_return).data, status=status.HTTP_200_OK
        )

    @extend_schema(
        request=RejectReturnSerializer,
        responses={200: ApprovalRequestSerializer},
        summary="Reject a return request (owner)",
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], permission_classes=[IsOwner])
    def reject(self, request, pk=None):
        approval = self._pending_return()
        serializer = RejectReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rejected = reject_return(
            approval=approval,
            owner=request.user,
            reviewer_note=serializer.validated_data.get("reviewer_note", ""),
            request=request,
        )
        return Response(ApprovalRequestSerializer(rejected).data)


@extend_schema_view(
    list=extend_schema(summary="List returns", tags=["Sales"]),
    retrieve=extend_schema(summary="Retrieve a return", tags=["Sales"]),
)
class SaleReturnViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = SaleReturnReadSerializer
    permission_classes = [IsAuthenticatedAndMFAVerified]
    queryset = SaleReturn.objects.select_related(
        "approval", "approved_by"
    ).prefetch_related("items", "refunds")
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False) or user.branch_id is None:
            return self.queryset.none()
        qs = self.queryset.filter(branch_id=user.branch_id)
        if not getattr(user, "is_owner", False):
            qs = qs.filter(approval__requested_by_id=user.id)
        return qs


# Convenience re-export for status checks in tests / callers.
__all__ = ["ApprovalStatus", "ApprovalViewSet", "SaleReturnViewSet"]
