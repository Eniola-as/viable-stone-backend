from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import Conflict
from apps.core.permissions import IsAuthenticatedAndMFAVerified
from apps.sales.api.discount_serializers import (
    DiscountRequestSerializer,
    DraftCartSerializer,
    DraftCreateSerializer,
    FinaliseDraftSerializer,
)
from apps.sales.api.return_serializers import (
    ApprovalRequestSerializer,
    ReturnRequestCreateSerializer,
)
from apps.sales.api.serializers import (
    CustomerListSerializer,
    CustomerSerializer,
    ReceiptSerializer,
    SaleCreateSerializer,
    SaleReadSerializer,
)
from apps.sales.models import Customer, Sale, SaleStatus
from apps.sales.selectors import sales_visible_to
from apps.sales.services import discounts as discount_service
from apps.sales.services.customers import resolve_customer
from apps.sales.services.receipts import receipt_context, render_receipt_pdf
from apps.sales.services.returns import RequestLine, submit_return_request
from apps.sales.services.sales import CartLine, PaymentLine, create_sale


@extend_schema_view(
    list=extend_schema(summary="List sales (own sales for cashiers)", tags=["Sales"]),
    retrieve=extend_schema(summary="Retrieve a sale", tags=["Sales"]),
    create=extend_schema(
        request=SaleCreateSerializer,
        responses={201: SaleReadSerializer},
        summary="Create a fully-paid sale (cash / transfer / POS / split)",
        tags=["Sales"],
    ),
)
class SaleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticatedAndMFAVerified]
    throttle_scope = "sales_write"
    ordering = ["-created_at"]
    # Real scoping is in get_queryset(); this only lets the schema type {id}.
    queryset = Sale.objects.none()

    def get_queryset(self):
        return sales_visible_to(self.request.user)

    def get_serializer_class(self):
        return SaleCreateSerializer if self.action == "create" else SaleReadSerializer

    def create(self, request, *args, **kwargs):
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        customer = resolve_customer(user.branch, data.get("customer"))
        sale = create_sale(
            branch=user.branch,
            cashier=user,
            cart=[
                CartLine(variant_id=i["variant"], quantity=i["quantity"])
                for i in data["items"]
            ],
            payments=[
                PaymentLine(
                    method=p["method"],
                    amount=p["amount"],
                    tendered_amount=p.get("tendered_amount"),
                    reference=p.get("reference", ""),
                )
                for p in data["payments"]
            ],
            client_sale_id=data["client_sale_id"],
            customer=customer,
            request=request,
        )
        out = SaleReadSerializer(sale, context={"request": request})
        return Response(out.data, status=status.HTTP_201_CREATED)

    def _completed_sale_or_conflict(self):
        sale = self.get_object()  # 404 via sales_visible_to (role + branch)
        if sale.status != SaleStatus.COMPLETED:
            raise Conflict(
                "A receipt is only available for a completed sale.",
                code="receipt_unavailable",
            )
        return sale

    @extend_schema(
        responses={200: ReceiptSerializer},
        summary="Structured receipt (snapshot data, no cost)",
        tags=["Sales"],
    )
    def receipt(self, request, pk=None):
        response = Response(receipt_context(self._completed_sale_or_conflict()))
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(
        request=ReturnRequestCreateSerializer,
        responses={201: ApprovalRequestSerializer},
        summary="Submit a return request for a completed sale",
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], url_path="return-requests")
    def return_requests(self, request, pk=None):
        # Any employee or the owner at the branch may raise a return request
        # (not only the cashier who rang the sale). Cross-branch id -> 404.
        from django.shortcuts import get_object_or_404

        sale = get_object_or_404(
            Sale.objects.filter(branch_id=request.user.branch_id), pk=pk
        )
        serializer = ReturnRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        approval = submit_return_request(
            sale=sale,
            requested_by=request.user,
            reason=data["reason"],
            lines=[
                RequestLine(sale_item_id=line["sale_item"], quantity=line["quantity"])
                for line in data["lines"]
            ],
            client_return_id=data["client_return_id"],
            request=request,
        )
        return Response(
            ApprovalRequestSerializer(approval).data, status=status.HTTP_201_CREATED
        )

    # --- Discount workflow on an internal DRAFT sale ------------------- #

    @extend_schema(
        request=DraftCreateSerializer,
        responses={201: SaleReadSerializer},
        summary="Create an internal DRAFT sale (not a quotation)",
        tags=["Sales"],
    )
    @action(detail=False, methods=["post"])
    def drafts(self, request):
        serializer = DraftCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        sale = discount_service.create_draft_sale(
            branch=request.user.branch,
            cashier=request.user,
            cart=[
                CartLine(variant_id=i["variant"], quantity=i["quantity"])
                for i in data["items"]
            ],
            client_sale_id=data["client_sale_id"],
            customer=resolve_customer(request.user.branch, data.get("customer")),
            request=request,
        )
        return Response(
            SaleReadSerializer(sale, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=DraftCartSerializer,
        responses={200: SaleReadSerializer},
        summary="Replace the cart of a DRAFT sale (invalidates any pending discount)",
        tags=["Sales"],
    )
    @action(detail=True, methods=["put"], url_path="draft-cart")
    def draft_cart(self, request, pk=None):
        serializer = DraftCartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale = discount_service.replace_draft_cart(
            sale=self.get_object(),
            cart=[
                CartLine(variant_id=i["variant"], quantity=i["quantity"])
                for i in serializer.validated_data["items"]
            ],
            request=request,
        )
        return Response(SaleReadSerializer(sale, context={"request": request}).data)

    @extend_schema(
        request=DiscountRequestSerializer,
        responses={201: ApprovalRequestSerializer},
        summary="Request an owner-approved fixed-Naira discount on a draft",
        tags=["Approvals"],
    )
    @action(detail=True, methods=["post"], url_path="discount-requests")
    def discount_requests(self, request, pk=None):
        serializer = DiscountRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approval = discount_service.request_discount(
            sale=self.get_object(),
            requested_by=request.user,
            amount=serializer.validated_data["amount"],
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(
            ApprovalRequestSerializer(approval).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        request=FinaliseDraftSerializer,
        responses={200: SaleReadSerializer},
        summary="Finalise an approved DRAFT into a completed, paid sale",
        tags=["Sales"],
    )
    @action(detail=True, methods=["post"])
    def finalise(self, request, pk=None):
        serializer = FinaliseDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        sale = discount_service.finalise_draft(
            sale=self.get_object(),
            cashier=request.user,
            payments=[
                PaymentLine(
                    method=p["method"],
                    amount=p["amount"],
                    tendered_amount=p.get("tendered_amount"),
                    reference=p.get("reference", ""),
                )
                for p in data["payments"]
            ],
            client_finalize_id=data.get("client_finalize_id"),
            request=request,
        )
        return Response(SaleReadSerializer(sale, context={"request": request}).data)

    @extend_schema(
        request=None,
        responses={200: SaleReadSerializer},
        summary="Cancel a DRAFT sale",
        tags=["Sales"],
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        sale = discount_service.cancel_draft(
            sale=self.get_object(), actor=request.user, request=request
        )
        return Response(SaleReadSerializer(sale, context={"request": request}).data)

    @extend_schema(
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
        summary="A4 paid-receipt PDF",
        tags=["Sales"],
    )
    def receipt_pdf(self, request, pk=None):
        sale = self._completed_sale_or_conflict()
        pdf = render_receipt_pdf(sale)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'inline; filename="{sale.receipt_number}.pdf"'
        )
        response["Content-Length"] = str(len(pdf))
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


@extend_schema_view(
    list=extend_schema(summary="List customers (phone masked)", tags=["Sales"]),
    retrieve=extend_schema(summary="Retrieve a customer", tags=["Sales"]),
    create=extend_schema(summary="Add customer details", tags=["Sales"]),
)
class CustomerViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticatedAndMFAVerified]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    search_fields = ["name", "phone"]
    ordering = ["name"]
    queryset = Customer.objects.none()

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False) or user.branch_id is None:
            return Customer.objects.none()
        return Customer.objects.filter(branch_id=user.branch_id)

    def get_serializer_class(self):
        if self.action == "list" and not getattr(self.request.user, "is_owner", False):
            return CustomerListSerializer
        return CustomerSerializer

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch)
