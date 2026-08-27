from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.sales.models import Customer, Payment, PaymentMethod, Sale, SaleItem
from apps.sales.services.customers import mask_phone

# --------------------------------------------------------------------------- #
# Customers                                                                   #
# --------------------------------------------------------------------------- #


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        if (
            not (attrs.get("name") or "").strip()
            and not (attrs.get("phone") or "").strip()
        ):
            raise serializers.ValidationError(
                "Provide a name or a phone number (or leave the sale as walk-in)."
            )
        return attrs


class CustomerListSerializer(serializers.ModelSerializer):
    """Broad list view — phone is masked for everyone."""

    phone = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "created_at"]
        read_only_fields = fields

    def get_phone(self, obj) -> str:
        return mask_phone(obj.phone)


class CustomerInlineSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(required=False, allow_blank=True, default="")


# --------------------------------------------------------------------------- #
# Sale input                                                                  #
# --------------------------------------------------------------------------- #


class SaleItemInputSerializer(serializers.Serializer):
    variant = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class PaymentInputSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    tendered_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    reference = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=150
    )


class SaleCreateSerializer(serializers.Serializer):
    client_sale_id = serializers.UUIDField()
    items = SaleItemInputSerializer(many=True)
    payments = PaymentInputSerializer(many=True)
    customer = CustomerInlineSerializer(required=False, allow_null=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Add at least one item.")
        return items

    def validate_payments(self, payments):
        if not payments:
            raise serializers.ValidationError("Add at least one payment.")
        return payments


# --------------------------------------------------------------------------- #
# Sale output                                                                 #
# --------------------------------------------------------------------------- #


class PaymentReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "method",
            "amount",
            "tendered_amount",
            "reference",
            "created_at",
        ]
        read_only_fields = fields


class SaleItemReadSerializer(serializers.ModelSerializer):
    """Cashier-safe: snapshots and price, never cost."""

    class Meta:
        model = SaleItem
        fields = [
            "id",
            "variant",
            "product_name_snapshot",
            "sku_snapshot",
            "variant_description_snapshot",
            "quantity",
            "unit_price_snapshot",
            "discount_amount",
            "line_total",
        ]
        read_only_fields = fields


class SaleItemOwnerSerializer(SaleItemReadSerializer):
    class Meta(SaleItemReadSerializer.Meta):
        fields = [*SaleItemReadSerializer.Meta.fields, "unit_cost_snapshot"]
        read_only_fields = fields


class SaleReadSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    payments = PaymentReadSerializer(many=True, read_only=True)
    cashier_username = serializers.CharField(source="cashier.username", read_only=True)
    customer_name = serializers.CharField(
        source="customer.name", read_only=True, default=None
    )

    class Meta:
        model = Sale
        fields = [
            "id",
            "receipt_number",
            "status",
            "source",
            "cashier",
            "cashier_username",
            "customer",
            "customer_name",
            "subtotal",
            "discount_total",
            "total",
            "change_due",
            "completed_at",
            "items",
            "payments",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(SaleItemOwnerSerializer(many=True))
    def get_items(self, sale):
        user = self.context["request"].user
        serializer_cls = (
            SaleItemOwnerSerializer
            if getattr(user, "is_owner", False)
            else SaleItemReadSerializer
        )
        return serializer_cls(sale.items.all(), many=True).data


# --------------------------------------------------------------------------- #
# Receipt (JSON) — documents the shape of receipt_context(); no cost / ids     #
# --------------------------------------------------------------------------- #


class _BusinessSerializer(serializers.Serializer):
    name = serializers.CharField()
    phone = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    address = serializers.CharField(allow_blank=True)


class _ReceiptItemSerializer(serializers.Serializer):
    description = serializers.CharField()
    sku = serializers.CharField()
    quantity = serializers.IntegerField()
    unit_price = serializers.CharField()
    line_total = serializers.CharField()


class _ReceiptPaymentSerializer(serializers.Serializer):
    method = serializers.CharField()
    label = serializers.CharField()
    amount = serializers.CharField()
    reference = serializers.CharField(allow_blank=True)
    tendered = serializers.CharField(required=False)
    change = serializers.CharField(required=False)


class _ReceiptPartySerializer(serializers.Serializer):
    name = serializers.CharField(allow_blank=True)
    code = serializers.CharField(required=False)
    phone = serializers.CharField(required=False, allow_blank=True)


class ReceiptSerializer(serializers.Serializer):
    business = _BusinessSerializer()
    currency = serializers.CharField()
    receipt_number = serializers.CharField()
    issued_at = serializers.CharField()
    issued_at_iso = serializers.DateTimeField()
    branch = _ReceiptPartySerializer()
    cashier = serializers.CharField()
    customer = _ReceiptPartySerializer(allow_null=True)
    items = _ReceiptItemSerializer(many=True)
    subtotal = serializers.CharField()
    discount_total = serializers.CharField()
    total = serializers.CharField()
    payments = _ReceiptPaymentSerializer(many=True)
    cash_tendered = serializers.CharField(allow_null=True)
    change_due = serializers.CharField()
    status = serializers.CharField()
    status_label = serializers.CharField()
