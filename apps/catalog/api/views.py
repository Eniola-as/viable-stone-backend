import mimetypes
import posixpath

from django.http import FileResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.catalog.api.filters import ProductFilterBackend
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
from apps.catalog.validators import sniff_image_type
from apps.core.mixins import AuditCreateUpdateMixin, BranchScopedQuerysetMixin
from apps.core.permissions import IsOwner, IsOwnerOrReadOnly
from apps.core.services.audit import record_audit

_CATALOGUE_METHODS = ["get", "post", "put", "patch", "head", "options"]


def _image_response(field_file) -> FileResponse:
    """Stream a stored image through its storage backend with safe headers.

    Uses only ``storage.open`` — never ``storage.path`` — so the same code path
    works for the local ``PrivateMediaStorage`` today and a private S3 bucket
    later. No filesystem path or storage key is exposed to the client.
    """

    name = field_file.name
    handle = field_file.storage.open(name, "rb")
    head = handle.read(32)
    handle.seek(0)
    content_type = sniff_image_type(head) or (
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    )

    response = FileResponse(handle, content_type=content_type)
    # Only the bare filename — never the storage key or an absolute path.
    response["Content-Disposition"] = f'inline; filename="{posixpath.basename(name)}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=300"
    return response


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
    list=extend_schema(
        summary="Search and filter products",
        description=(
            "Filter with `category` / `brand` (exact id), `kind` "
            "(`PAINT` | `EQUIPMENT`) and `is_active` (`true` | `false`); "
            "combine freely with `search`, `ordering`, `page` and `page_size`. "
            "Filters are AND-combined and always scoped to the caller's branch."
        ),
        tags=["Catalogue"],
    ),
    retrieve=extend_schema(summary="Retrieve a product", tags=["Catalogue"]),
    create=extend_schema(
        summary="Create a product with an optional image (owner)", tags=["Catalogue"]
    ),
)
class ProductViewSet(_CatalogueViewSet):
    queryset = Product.objects.select_related("brand", "category").prefetch_related(
        "variants"
    )
    filter_backends = [
        ProductFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    search_fields = ["name", "variants__sku", "variants__barcode", "brand__name"]
    ordering_fields = ["name", "created_at"]
    audit_action_prefix = "product"

    def get_serializer_class(self):
        if self.action in ("list", "retrieve"):
            return ProductReadSerializer
        return ProductWriteSerializer

    # --- product image: authenticated, branch-scoped, storage-backed ------- #
    # Wired via an explicit path() in api/urls.py (GET + DELETE) so the image
    # bytes are reachable ONLY here, never through a public media URL.

    @extend_schema(
        responses={(200, "image/*"): OpenApiTypes.BINARY},
        summary="Download a product's image (owner or branch employee)",
        tags=["Catalogue"],
    )
    def image(self, request, *args, **kwargs):
        product = self.get_object()  # cross-branch / unknown id -> 404
        if not product.image:
            raise NotFound("This product has no image.")
        return _image_response(product.image)

    @extend_schema(
        responses={204: None},
        summary="Clear a product's image (owner)",
        tags=["Catalogue"],
    )
    def image_clear(self, request, *args, **kwargs):
        product = self.get_object()
        if not product.image:
            raise NotFound("This product has no image.")
        previous = product.image.name
        product.image.delete(save=False)
        product.image = ""
        product.save(update_fields=["image", "updated_at"])
        record_audit(
            action="product.image_clear",
            target=product,
            request=request,
            before={"image": previous},
        )
        return Response(status=204)


@extend_schema_view(
    list=extend_schema(
        summary="Search variants (name, SKU, barcode, colour, size, finish, "
        "brand, category)",
        tags=["Catalogue"],
    ),
    retrieve=extend_schema(summary="Retrieve a variant", tags=["Catalogue"]),
    create=extend_schema(summary="Create a variant (owner)", tags=["Catalogue"]),
)
class ProductVariantViewSet(AuditCreateUpdateMixin, viewsets.ModelViewSet):
    permission_classes = [IsOwnerOrReadOnly]
    http_method_names = _CATALOGUE_METHODS
    queryset = ProductVariant.objects.select_related(
        "product", "product__brand", "product__category"
    )
    search_fields = [
        "product__name",
        "sku",
        "barcode",
        "colour",
        "size",
        "finish",
        "product__brand__name",
        "product__category__name",
    ]
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
