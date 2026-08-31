from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)
from apps.sales.models import (
    ApprovalRequest,
    ApprovalType,
    Customer,
    Payment,
    PaymentMethod,
    Sale,
    SaleItem,
)
from apps.sales.services.customers import mask_phone

# --------------------------------------------------------------------------- #
# Customers                                                                   #
# --------------------------------------------------------------------------- #


class CustomerSerializer(ControlCharSafeModelSerializer):
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


class CustomerListSerializer(ControlCharSafeModelSerializer):
    """Broad list view — phone is masked for everyone."""

    phone = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "created_at"]
        read_only_fields = fields

    def get_phone(self, obj) -> str:
        return mask_phone(obj.phone)


class CustomerInlineSerializer(ControlCharSafeSerializer):
    name = serializers.CharField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(required=False, allow_blank=True, default="")


# --------------------------------------------------------------------------- #
# Sale input                                                                  #
# --------------------------------------------------------------------------- #


class SaleItemInputSerializer(ControlCharSafeSerializer):
    variant = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class PaymentInputSerializer(ControlCharSafeSerializer):
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


class SaleCreateSerializer(ControlCharSafeSerializer):
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


class PaymentReadSerializer(ControlCharSafeModelSerializer):
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


class SaleItemReadSerializer(ControlCharSafeModelSerializer):
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


class SaleDiscountRequestSummarySerializer(ControlCharSafeModelSerializer):
    """G22 — minimal, read-only view of a sale's DISCOUNT ``ApprovalRequest``.

    Only DISCOUNT-type requests reach here. Deliberately excludes the draft
    ``fingerprint``, the ``subtotal`` snapshot, raw user ids and every
    owner-only financial field. ``status`` reuses the canonical
    ``ApprovalStatusEnum`` (``PENDING`` / ``APPROVED`` / ``REJECTED``) — the
    model has no ``CANCELLED``/``EXPIRED`` state, so a request voided by a cart
    edit or a draft cancel is a ``REJECTED`` row with ``reviewed_by_username``
    ``null`` and a ``reviewer_note`` that starts ``"Superseded: "``.
    """

    requested_amount = serializers.SerializerMethodField()
    approved_amount = serializers.SerializerMethodField()
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True
    )
    reviewed_by_username = serializers.CharField(
        source="reviewed_by.username", read_only=True, default=None
    )

    class Meta:
        model = ApprovalRequest
        fields = [
            "id",
            "status",
            "requested_amount",
            "approved_amount",
            "reason",
            "reviewer_note",
            "requested_by_username",
            "reviewed_by_username",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_requested_amount(self, obj) -> str | None:
        # Always set by request_discount(); a decimal string like "1500.00".
        return (obj.requested_changes or {}).get("requested_amount")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_approved_amount(self, obj) -> str | None:
        # Only present once an owner has APPROVED the request.
        return (obj.requested_changes or {}).get("approved_amount")


class SaleReadSerializer(ControlCharSafeModelSerializer):
    items = serializers.SerializerMethodField()
    payments = PaymentReadSerializer(many=True, read_only=True)
    cashier_username = serializers.CharField(source="cashier.username", read_only=True)
    customer_name = serializers.CharField(
        source="customer.name", read_only=True, default=None
    )
    discount_request = serializers.SerializerMethodField()

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
            "discount_request",
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

    @extend_schema_field(SaleDiscountRequestSummarySerializer(allow_null=True))
    def get_discount_request(self, sale):
        # ``_discount_requests`` is prefetched by ``sales_visible_to`` (DISCOUNT
        # only, newest first, requested_by/reviewed_by joined) so a sale list is
        # never N+1. Fall back to a scoped query for objects built elsewhere.
        rows = getattr(sale, "_discount_requests", None)
        if rows is None:
            rows = list(
                sale.approval_requests.filter(request_type=ApprovalType.DISCOUNT)
                .select_related("requested_by", "reviewed_by")
                .order_by("-created_at")
            )
        if not rows:
            return None
        return SaleDiscountRequestSummarySerializer(rows[0]).data


# --------------------------------------------------------------------------- #
# Receipt (JSON) — documents the shape of receipt_context(); no cost / ids     #
# --------------------------------------------------------------------------- #


class _BusinessSerializer(ControlCharSafeSerializer):
    name = serializers.CharField()
    phone = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    address = serializers.CharField(allow_blank=True)


class _ReceiptItemSerializer(ControlCharSafeSerializer):
    description = serializers.CharField()
    sku = serializers.CharField()
    quantity = serializers.IntegerField()
    unit_price = serializers.CharField()
    line_total = serializers.CharField()


class _ReceiptPaymentSerializer(ControlCharSafeSerializer):
    method = serializers.CharField()
    label = serializers.CharField()
    amount = serializers.CharField()
    reference = serializers.CharField(allow_blank=True)
    tendered = serializers.CharField(required=False)
    change = serializers.CharField(required=False)


class _ReceiptPartySerializer(ControlCharSafeSerializer):
    name = serializers.CharField(allow_blank=True)
    code = serializers.CharField(required=False)
    phone = serializers.CharField(required=False, allow_blank=True)


class ReceiptSerializer(ControlCharSafeSerializer):
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
