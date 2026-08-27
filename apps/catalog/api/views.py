from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.catalog.api.serializers import (
    BrandSerializer,
    CategorySerializer,
    PriceHistorySerializer,
    ProductReadSerializer,
    ProductVariantReadSerializer,
    ProductVariantWriteSerializer,
    ProductWriteSerializer,
    SetPriceSerializer,
)
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.catalog.selectors import current_price
from apps.catalog.services.pricing import set_active_price
from apps.core.mixins import AuditCreateUpdateMixin, BranchScopedQuerysetMixin
from apps.core.permissions import IsOwner, IsOwnerOrReadOnly

_CATALOGUE_METHODS = ["get", "post", "put", "patch", "head", "options"]


class _CatalogueViewSet(
    BranchScopedQuerysetMixin, AuditCreateUpdateMixin, viewsets.ModelViewSet
):
    """Owner writes, everyone (MFA-cleared) reads their branch. No hard deletes."""

    permission_classes = [IsOwnerOrReadOnly]
    http_method_names = _CATALOGUE_METHODS

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.user.is_owner:
            queryset = queryset.filter(is_active=True)
        return queryset

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch)
        super().perform_create(serializer)


@extend_schema_view(
    list=extend_schema(summary="List categories", tags=["Catalogue"]),
    create=extend_schema(summary="Create a category (owner)", tags=["Catalogue"]),
)
class CategoryViewSet(_CatalogueViewSet):
    serializer_class = CategorySerializer
    queryset = Category.objects.all()
    search_fields = ["name"]
    audit_action_prefix = "category"


@extend_schema_view(
    list=extend_schema(summary="List brands", tags=["Catalogue"]),
    create=extend_schema(summary="Create a brand (owner)", tags=["Catalogue"]),
)
class BrandViewSet(_CatalogueViewSet):
    serializer_class = BrandSerializer
    queryset = Brand.objects.all()
    search_fields = ["name"]
    audit_action_prefix = "brand"


@extend_schema_view(
    list=extend_schema(summary="Search products", tags=["Catalogue"]),
    retrieve=extend_schema(summary="Retrieve a product", tags=["Catalogue"]),
    create=extend_schema(summary="Create a product (owner)", tags=["Catalogue"]),
)
class ProductViewSet(_CatalogueViewSet):
    queryset = Product.objects.select_related("brand", "category").prefetch_related(
        "variants"
    )
    search_fields = ["name", "variants__sku", "variants__barcode", "brand__name"]
    ordering_fields = ["name", "created_at"]
    audit_action_prefix = "product"

    def get_serializer_class(self):
        if self.action in ("list", "retrieve"):
            return ProductReadSerializer
        return ProductWriteSerializer


@extend_schema_view(
    list=extend_schema(summary="Search variants (by SKU/barcode)", tags=["Catalogue"]),
    retrieve=extend_schema(summary="Retrieve a variant", tags=["Catalogue"]),
    create=extend_schema(summary="Create a variant (owner)", tags=["Catalogue"]),
)
class ProductVariantViewSet(AuditCreateUpdateMixin, viewsets.ModelViewSet):
    permission_classes = [IsOwnerOrReadOnly]
    http_method_names = _CATALOGUE_METHODS
    queryset = ProductVariant.objects.select_related(
        "product", "product__brand", "product__category"
    )
    search_fields = ["sku", "barcode", "product__name"]
    ordering_fields = ["sku", "created_at"]
    audit_action_prefix = "variant"

    def get_serializer_class(self):
        if self.action in ("list", "retrieve"):
            return ProductVariantReadSerializer
        return ProductVariantWriteSerializer

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False):
            return self.queryset.none()
        queryset = self.queryset.filter(product__branch_id=user.branch_id)
        if not user.is_owner:
            queryset = queryset.filter(is_active=True, product__is_active=True)
        product = self.request.query_params.get("product")
        if product:
            queryset = queryset.filter(product_id=product)
        return queryset

    @extend_schema(
        request=SetPriceSerializer,
        responses={200: PriceHistorySerializer},
        summary="Set the current selling price (owner)",
        tags=["Catalogue"],
    )
    @action(
        detail=True, methods=["post"], permission_classes=[IsOwner], url_path="price"
    )
    def price(self, request, pk=None):
        variant = self.get_object()
        serializer = SetPriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = set_active_price(
            variant=variant,
            amount=serializer.validated_data["amount"],
            changed_by=request.user,
            request=request,
        )
        return Response(PriceHistorySerializer(row).data)

    @extend_schema(
        responses={200: PriceHistorySerializer(many=True)},
        summary="Price history for a variant (owner)",
        tags=["Catalogue"],
    )
    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsOwner],
        url_path="price-history",
    )
    def price_history(self, request, pk=None):
        variant = self.get_object()
        rows = variant.price_history.select_related("changed_by").order_by(
            "-valid_from"
        )
        page = self.paginate_queryset(rows)
        if page is not None:
            return self.get_paginated_response(
                PriceHistorySerializer(page, many=True).data
            )
        return Response(PriceHistorySerializer(rows, many=True).data)

    @extend_schema(
        responses={200: PriceHistorySerializer},
        summary="Current price for a variant",
        tags=["Catalogue"],
    )
    @action(detail=True, methods=["get"], url_path="current-price")
    def current_price_view(self, request, pk=None):
        row = current_price(self.get_object())
        if row is None:
            return Response({"detail": "No price set."}, status=404)
        return Response(PriceHistorySerializer(row).data)
