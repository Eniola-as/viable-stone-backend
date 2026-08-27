from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from apps.core.exceptions import Conflict
from apps.core.permissions import IsAuthenticatedAndMFAVerified
from apps.sales.api.serializers import (
    CustomerListSerializer,
    CustomerSerializer,
    ReceiptSerializer,
    SaleCreateSerializer,
    SaleReadSerializer,
)
from apps.sales.models import Customer, Sale, SaleStatus
from apps.sales.selectors import sales_visible_to
from apps.sales.services.customers import resolve_customer
from apps.sales.services.receipts import receipt_context, render_receipt_pdf
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
