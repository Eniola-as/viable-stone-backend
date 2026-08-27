from decimal import Decimal

from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.catalog.selectors import current_price
from apps.core.money import to_money
from apps.inventory.models import (
    InventoryBalance,
    Restock,
    RestockItem,
    StockCount,
    StockCountItem,
    StockMovement,
    Supplier,
)

_MONEY_FIELD = serializers.DecimalField(
    max_digits=18, decimal_places=2, allow_null=True
)


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "id",
            "name",
            "contact_name",
            "phone",
            "email",
            "address",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


# --- Restock ------------------------------------------------------------- #


class RestockItemWriteSerializer(serializers.Serializer):
    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    unit_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )


class RestockItemReadSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = RestockItem
        fields = ["id", "variant", "variant_sku", "quantity", "unit_cost", "line_total"]
        read_only_fields = fields


class RestockWriteSerializer(serializers.ModelSerializer):
    items = RestockItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = Restock
        fields = [
            "id",
            "supplier",
            "supplier_invoice_number",
            "invoice_file",
            "date",
            "items",
        ]
        read_only_fields = ["id"]

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("At least one item is required.")
        seen = set()
        for item in items:
            vid = item["variant"].pk
            if vid in seen:
                raise serializers.ValidationError(
                    "Duplicate variant in items; merge the lines."
                )
            seen.add(vid)
        return items

    def validate(self, attrs):
        request = self.context["request"]
        branch_id = request.user.branch_id
        if attrs["supplier"].branch_id != branch_id:
            raise serializers.ValidationError(
                {"supplier": ["Unknown supplier for this branch."]}
            )
        for item in attrs["items"]:
            if item["variant"].product.branch_id != branch_id:
                raise serializers.ValidationError(
                    {"items": ["A variant does not belong to this branch."]}
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("items")
        request = self.context["request"]
        restock = Restock.objects.create(
            branch=request.user.branch,
            created_by=request.user,
            **validated_data,
        )
        RestockItem.objects.bulk_create(
            [
                RestockItem(
                    restock=restock,
                    variant=item["variant"],
                    quantity=item["quantity"],
                    unit_cost=item["unit_cost"],
                    line_total=to_money(Decimal(item["quantity"]) * item["unit_cost"]),
                )
                for item in items
            ]
        )
        return restock


class RestockReadSerializer(serializers.ModelSerializer):
    items = RestockItemReadSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)

    class Meta:
        model = Restock
        fields = [
            "id",
            "supplier",
            "supplier_name",
            "supplier_invoice_number",
            "invoice_file",
            "date",
            "status",
            "total_cost",
            "created_by",
            "confirmed_by",
            "confirmed_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# --- Inventory balances ------------------------------------------------- #


class _BaseBalanceSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    product_name = serializers.CharField(source="variant.product.name", read_only=True)
    variant_description = serializers.CharField(
        source="variant.description", read_only=True
    )
    low_stock_level = serializers.IntegerField(
        source="variant.low_stock_level", read_only=True
    )
    is_low = serializers.SerializerMethodField()

    def get_is_low(self, balance) -> bool:
        level = balance.variant.low_stock_level
        return level > 0 and balance.quantity <= level


class InventoryBalanceEmployeeSerializer(_BaseBalanceSerializer):
    """Cashier view: quantity + selling price, never cost."""

    price = serializers.SerializerMethodField()

    class Meta:
        model = InventoryBalance
        fields = [
            "id",
            "variant",
            "variant_sku",
            "product_name",
            "variant_description",
            "quantity",
            "low_stock_level",
            "is_low",
            "price",
            "last_restocked_at",
        ]
        read_only_fields = fields

    @extend_schema_field(_MONEY_FIELD)
    def get_price(self, balance):
        row = current_price(balance.variant)
        return str(row.amount) if row else None


class InventoryBalanceOwnerSerializer(_BaseBalanceSerializer):
    """Owner view: adds weighted-average cost and stock value."""

    stock_value = serializers.SerializerMethodField()

    class Meta:
        model = InventoryBalance
        fields = [
            "id",
            "variant",
            "variant_sku",
            "product_name",
            "variant_description",
            "quantity",
            "average_unit_cost",
            "stock_value",
            "low_stock_level",
            "is_low",
            "last_restocked_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(_MONEY_FIELD)
    def get_stock_value(self, balance):
        return str(to_money(Decimal(balance.quantity) * balance.average_unit_cost))


class StockMovementSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "variant",
            "variant_sku",
            "movement_type",
            "quantity_delta",
            "unit_cost_snapshot",
            "reference_type",
            "reference_id",
            "reason",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class OpeningStockSerializer(serializers.Serializer):
    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    unit_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )


class StockAdjustmentSerializer(serializers.Serializer):
    variant = serializers.UUIDField()
    direction = serializers.ChoiceField(choices=["INCREASE", "DECREASE"])
    quantity = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=10, max_length=2000)
    client_adjustment_id = serializers.UUIDField()


# --- Stock counts ----------------------------------------------------- #


class StockCountItemWriteSerializer(serializers.Serializer):
    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    counted_quantity = serializers.IntegerField(min_value=0)


class StockCountItemReadSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = StockCountItem
        fields = [
            "id",
            "variant",
            "variant_sku",
            "system_quantity_snapshot",
            "counted_quantity",
            "variance",
        ]
        read_only_fields = fields


class StockCountWriteSerializer(serializers.ModelSerializer):
    items = StockCountItemWriteSerializer(many=True, write_only=True)

    class Meta:
        model = StockCount
        fields = ["id", "reason", "items"]
        read_only_fields = ["id"]

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("At least one counted item is required.")
        if len({i["variant"].pk for i in items}) != len(items):
            raise serializers.ValidationError("Duplicate variant in items.")
        return items

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("items")
        request = self.context["request"]
        branch = request.user.branch
        count = StockCount.objects.create(
            branch=branch, created_by=request.user, **validated_data
        )
        balances = {
            b.variant_id: b.quantity
            for b in InventoryBalance.objects.filter(
                branch=branch, variant__in=[i["variant"] for i in items]
            )
        }
        StockCountItem.objects.bulk_create(
            [
                StockCountItem(
                    stock_count=count,
                    variant=item["variant"],
                    system_quantity_snapshot=balances.get(item["variant"].pk, 0),
                    counted_quantity=item["counted_quantity"],
                    variance=item["counted_quantity"]
                    - balances.get(item["variant"].pk, 0),
                )
                for item in items
            ]
        )
        return count


class StockCountReadSerializer(serializers.ModelSerializer):
    items = StockCountItemReadSerializer(many=True, read_only=True)

    class Meta:
        model = StockCount
        fields = [
            "id",
            "status",
            "reason",
            "created_by",
            "submitted_at",
            "applied_by",
            "applied_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
