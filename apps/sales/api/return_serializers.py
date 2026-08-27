from decimal import Decimal

from rest_framework import serializers

from apps.sales.models import (
    ApprovalRequest,
    PaymentMethod,
    Refund,
    ReturnCondition,
    SaleReturn,
    SaleReturnItem,
)

_MONEY = {"max_digits": 14, "decimal_places": 2}


# --- Submit a return request ----------------------------------------- #


class ReturnRequestLineSerializer(serializers.Serializer):
    sale_item = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class ReturnRequestCreateSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    client_return_id = serializers.UUIDField()
    lines = ReturnRequestLineSerializer(many=True)

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("Add at least one line.")
        return lines


# --- Approve / reject --------------------------------------------- #


class ApprovedLineSerializer(serializers.Serializer):
    sale_item = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)
    condition = serializers.ChoiceField(choices=ReturnCondition.choices)


class RefundLineSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount = serializers.DecimalField(min_value=Decimal("0.01"), **_MONEY)
    reference = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=150
    )


class ApproveReturnSerializer(serializers.Serializer):
    reviewer_note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000
    )
    client_return_id = serializers.UUIDField(required=False)
    lines = ApprovedLineSerializer(many=True)
    refunds = RefundLineSerializer(many=True)

    def validate(self, attrs):
        if not attrs.get("lines"):
            raise serializers.ValidationError({"lines": ["At least one line."]})
        if not attrs.get("refunds"):
            raise serializers.ValidationError({"refunds": ["At least one refund."]})
        return attrs


class RejectReturnSerializer(serializers.Serializer):
    reviewer_note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000
    )


# --- Read representations -------------------------------------- #


class ApprovalRequestSerializer(serializers.ModelSerializer):
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True
    )

    class Meta:
        model = ApprovalRequest
        fields = [
            "id",
            "request_type",
            "status",
            "sale",
            "variant",
            "stock_count",
            "reason",
            "requested_changes",
            "requested_by",
            "requested_by_username",
            "reviewed_by",
            "reviewer_note",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RefundReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = ["id", "method", "amount", "reference", "issued_by", "created_at"]
        read_only_fields = fields


class SaleReturnItemReadSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(
        source="original_sale_item.sku_snapshot", read_only=True
    )
    product_name = serializers.CharField(
        source="original_sale_item.product_name_snapshot", read_only=True
    )

    class Meta:
        model = SaleReturnItem
        fields = [
            "id",
            "original_sale_item",
            "sku",
            "product_name",
            "quantity",
            "condition",
            "unit_price_snapshot",
            "unit_cost_snapshot",
            "line_total",
        ]
        read_only_fields = fields


class SaleReturnReadSerializer(serializers.ModelSerializer):
    items = SaleReturnItemReadSerializer(many=True, read_only=True)
    refunds = RefundReadSerializer(many=True, read_only=True)

    class Meta:
        model = SaleReturn
        fields = [
            "id",
            "original_sale",
            "approval",
            "status",
            "total",
            "client_return_id",
            "approved_by",
            "completed_at",
            "items",
            "refunds",
            "created_at",
        ]
        read_only_fields = fields
