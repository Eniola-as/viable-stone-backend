from decimal import Decimal

from rest_framework import serializers

from apps.sales.models import PaymentMethod


class _CartLineSerializer(serializers.Serializer):
    variant = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class DraftCreateSerializer(serializers.Serializer):
    client_sale_id = serializers.UUIDField()
    items = _CartLineSerializer(many=True)
    customer = serializers.DictField(required=False)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Add at least one item.")
        return items


class DraftCartSerializer(serializers.Serializer):
    items = _CartLineSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Add at least one item.")
        return items


class DiscountRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reason = serializers.CharField(max_length=2000)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A reason is required.")
        return value


class ApproveDiscountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reviewer_note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000
    )


class _PaymentLineSerializer(serializers.Serializer):
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


class FinaliseDraftSerializer(serializers.Serializer):
    payments = _PaymentLineSerializer(many=True)
    client_finalize_id = serializers.UUIDField(required=False)

    def validate_payments(self, payments):
        if not payments:
            raise serializers.ValidationError("Add at least one payment.")
        return payments
