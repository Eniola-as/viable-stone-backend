from decimal import Decimal

from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalog.models import Brand, Category, PriceHistory, Product, ProductVariant
from apps.catalog.selectors import current_price
from apps.catalog.validators import validate_product_image
from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)


class CategorySerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class BrandSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductVariantWriteSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "colour",
            "size",
            "finish",
            "sku",
            "barcode",
            "low_stock_level",
            "is_active",
        ]
        read_only_fields = ["id"]

    def validate_product(self, product):
        request = self.context["request"]
        if product.branch_id != request.user.branch_id:
            raise serializers.ValidationError("Unknown product for this branch.")
        return product


class PriceHistorySerializer(ControlCharSafeModelSerializer):
    changed_by_username = serializers.CharField(
        source="changed_by.username", read_only=True
    )

    class Meta:
        model = PriceHistory
        fields = [
            "id",
            "amount",
            "valid_from",
            "valid_to",
            "changed_by",
            "changed_by_username",
            "created_at",
        ]
        read_only_fields = fields


class ProductVariantReadSerializer(ControlCharSafeModelSerializer):
    description = serializers.CharField(read_only=True)
    price = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "colour",
            "size",
            "finish",
            "description",
            "sku",
            "barcode",
            "low_stock_level",
            "is_active",
            "price",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_price(self, variant) -> str | None:
        row = current_price(variant)
        return str(row.amount) if row else None


class ProductWriteSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = Product
        fields = [
            "id",
            "category",
            "brand",
            "kind",
            "name",
            "description",
            "image",
            "is_active",
        ]
        read_only_fields = ["id"]

    def validate_image(self, image):
        # ``multipart/form-data`` only. Signature-checked: JPEG/PNG/WebP, ≤5 MB,
        # extension must match real content. A disguised PDF is rejected here.
        if image:
            validate_product_image(image)
        return image

    def validate(self, attrs):
        request = self.context["request"]
        branch_id = request.user.branch_id
        category = attrs.get("category") or getattr(self.instance, "category", None)
        brand = attrs.get("brand", getattr(self.instance, "brand", None))
        if category is not None and category.branch_id != branch_id:
            raise serializers.ValidationError(
                {"category": ["Unknown category for this branch."]}
            )
        if brand is not None and brand.branch_id != branch_id:
            raise serializers.ValidationError(
                {"brand": ["Unknown brand for this branch."]}
            )
        return attrs


class ProductReadSerializer(ControlCharSafeModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    brand_name = serializers.CharField(
        source="brand.name", read_only=True, default=None
    )
    variants = ProductVariantReadSerializer(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "category",
            "category_name",
            "brand",
            "brand_name",
            "kind",
            "name",
            "description",
            "image",
            "image_url",
            "is_active",
            "variants",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field({"type": "string", "format": "uri", "nullable": True})
    def get_image_url(self, product) -> str | None:
        """Stable authenticated URL for the image bytes, or ``None``.

        The raw ``image`` field is the private storage reference and is not
        directly fetchable; clients load the picture from here.
        """

        if not product.image:
            return None
        path = reverse("api:product-image", kwargs={"pk": product.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request is not None else path


class SetPriceSerializer(ControlCharSafeSerializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
