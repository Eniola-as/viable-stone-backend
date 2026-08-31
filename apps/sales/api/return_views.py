from drf_spectacular.utils import (
    PolymorphicProxySerializer,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsAuthenticatedAndMFAVerified, IsOwner
from apps.sales.api.discount_serializers import ApproveDiscountSerializer
from apps.sales.api.list_filters import ApprovalFilterBackend
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
from apps.sales.services.discounts import approve_discount, reject_discount
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    approve_return,
    reject_return,
)

# ``POST /api/v1/approvals/{id}/approve/`` is polymorphic: the body and the
# response depend on the approval's ``request_type`` (read it from the list /
# detail representation first). The path resource is the discriminator, so this
# is a plain ``oneOf`` with no in-body type field.
_APPROVE_REQUEST = PolymorphicProxySerializer(
    component_name="ApproveRequest",
    serializers={
        ApprovalType.RETURN.value: ApproveReturnSerializer,
        ApprovalType.DISCOUNT.value: ApproveDiscountSerializer,
    },
    resource_type_field_name=None,
)
_APPROVE_RESPONSE = PolymorphicProxySerializer(
    component_name="ApproveResult",
    serializers={
        ApprovalType.RETURN.value: SaleReturnReadSerializer,
        ApprovalType.DISCOUNT.value: ApprovalRequestSerializer,
    },
    resource_type_field_name=None,
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
    filter_backends = [
        ApprovalFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False) or user.branch_id is None:
            return self.queryset.none()
        qs = self.queryset.filter(branch_id=user.branch_id)
        if not getattr(user, "is_owner", False):
            qs = qs.filter(requested_by_id=user.id)
        return qs

    @extend_schema(
        request=_APPROVE_REQUEST,
        responses={200: _APPROVE_RESPONSE},
        summary="Approve a pending request (owner)",
        description=(
            "Polymorphic on the approval's `request_type` (read it from "
            "`GET /api/v1/approvals/{id}/` first):\n\n"
            "* `DISCOUNT` — body `{amount, reviewer_note?}`; returns the updated "
            "`ApprovalRequest`.\n"
            "* `RETURN` — body `{lines[], refunds[], reviewer_note?, "
            "client_return_id?}`; returns the created `SaleReturn`.\n\n"
            "Sending the wrong body yields `400` naming the missing field(s) "
            "(`amount`, or `lines`/`refunds`)."
        ),
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], permission_classes=[IsOwner])
    def approve(self, request, pk=None):
        approval = self.get_object()
        if approval.request_type == ApprovalType.DISCOUNT:
            serializer = ApproveDiscountSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            approved = approve_discount(
                approval=approval,
                owner=request.user,
                amount=serializer.validated_data["amount"],
                reviewer_note=serializer.validated_data.get("reviewer_note", ""),
                request=request,
            )
            return Response(ApprovalRequestSerializer(approved).data)

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
        summary="Reject a pending request (owner) — return or discount",
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], permission_classes=[IsOwner])
    def reject(self, request, pk=None):
        approval = self.get_object()
        serializer = RejectReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("reviewer_note", "")
        reject = (
            reject_discount
            if approval.request_type == ApprovalType.DISCOUNT
            else reject_return
        )
        rejected = reject(
            approval=approval, owner=request.user, reviewer_note=note, request=request
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
