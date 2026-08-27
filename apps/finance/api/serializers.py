from decimal import Decimal

from rest_framework import serializers

from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)
from apps.finance.models import Expense, ExpenseCategory

_MONEY = {"max_digits": 18, "decimal_places": 2}


class ExpenseCategorySerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class ExpenseSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = Expense
        fields = [
            "id",
            "category",
            "amount",
            "expense_date",
            "description",
            "receipt_file",
            "created_by",
            "is_voided",
            "void_reason",
            "voided_by",
            "voided_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_by",
            "is_voided",
            "void_reason",
            "voided_by",
            "voided_at",
            "created_at",
            "updated_at",
        ]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than zero.")
        return value

    def validate_receipt_file(self, value):
        if value:
            from apps.core.validators import validate_private_upload

            validate_private_upload(value)
        return value

    def validate_category(self, category):
        request = self.context["request"]
        if category.branch_id != request.user.branch_id:
            raise serializers.ValidationError("Unknown category for this branch.")
        return category

    def validate(self, attrs):
        # The recorded amount is never overwritten; edit metadata only, then void.
        if self.instance is not None:
            if self.instance.is_voided:
                raise serializers.ValidationError("A voided expense cannot be edited.")
            attrs.pop("amount", None)
        return attrs


class ExpenseVoidSerializer(ControlCharSafeSerializer):
    reason = serializers.CharField(max_length=500)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A void reason is required.")
        return value


# --- Report response schemas ------------------------------------------- #


class ProfitReportSerializer(ControlCharSafeSerializer):
    period = serializers.CharField()
    start = serializers.DateField()
    end = serializers.DateField()
    currency = serializers.CharField()
    revenue = serializers.DecimalField(**_MONEY)
    cogs = serializers.DecimalField(**_MONEY)
    gross_profit = serializers.DecimalField(**_MONEY)
    returns_total = serializers.DecimalField(**_MONEY)
    cost_reversed = serializers.DecimalField(**_MONEY)
    expenses_total = serializers.DecimalField(**_MONEY)
    net_profit = serializers.DecimalField(**_MONEY)
    sales_count = serializers.IntegerField()


class BestSellerSerializer(ControlCharSafeSerializer):
    variant = serializers.UUIDField()
    sku = serializers.CharField()
    product_name = serializers.CharField()
    quantity_sold = serializers.IntegerField()
    revenue = serializers.DecimalField(**_MONEY)


class SlowMoverSerializer(ControlCharSafeSerializer):
    variant = serializers.UUIDField()
    sku = serializers.CharField()
    product_name = serializers.CharField()
    quantity_in_stock = serializers.IntegerField()
    quantity_sold = serializers.IntegerField()


class InventorySummarySerializer(ControlCharSafeSerializer):
    stock_value = serializers.DecimalField(**_MONEY)
    low_stock_count = serializers.IntegerField()
    distinct_variants_in_stock = serializers.IntegerField()
    total_units_in_stock = serializers.IntegerField()


DEFAULT_REVENUE = Decimal("0.00")
