from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import OfflineDeviceAuthorization, RegisteredDevice
from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)
from apps.sales.models import OfflineSaleSyncRecord, PaymentMethod


class OfflineDeviceSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = RegisteredDevice
        fields = [
            "id",
            "device_id",
            "name",
            "status",
            "registered_at",
            "last_seen_at",
            "revoked_at",
        ]
        read_only_fields = fields


class OfflineDeviceCreateSerializer(ControlCharSafeSerializer):
    name = serializers.CharField(max_length=100)


class OfflineAuthorizationSerializer(ControlCharSafeModelSerializer):
    device_name = serializers.CharField(source="device.name", read_only=True)
    cashier_username = serializers.CharField(source="cashier.username", read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = OfflineDeviceAuthorization
        fields = [
            "id",
            "status",
            "device",
            "device_name",
            "cashier",
            "cashier_username",
            "snapshot_version",
            "issued_at",
            "expires_at",
            "is_expired",
            "signed_token",
            "ended_at",
            "end_reason",
            "created_at",
        ]
        read_only_fields = fields


class OfflineAuthorizationCreateSerializer(ControlCharSafeSerializer):
    device = serializers.UUIDField()
    cashier = serializers.UUIDField()


class OfflineAuthorizationRevokeSerializer(ControlCharSafeSerializer):
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=300
    )


class OfflineAuthorizationReplaceSerializer(ControlCharSafeSerializer):
    device = serializers.UUIDField(required=False)
    cashier = serializers.UUIDField(required=False)


class OfflineSessionEndSerializer(ControlCharSafeSerializer):
    force = serializers.BooleanField(default=False)
    mfa_confirmed = serializers.BooleanField(default=False)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=300
    )


class OfflineAuthorizationStatusSerializer(ControlCharSafeSerializer):
    authorization_id = serializers.UUIDField()
    status = serializers.CharField()
    expires_at = serializers.DateTimeField()
    is_expired = serializers.BooleanField()
    snapshot_version = serializers.IntegerField()
    synced_count = serializers.IntegerField()
    pending_review_count = serializers.IntegerField()


class _OfflineItemSerializer(ControlCharSafeSerializer):
    variant_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class _OfflinePaymentSerializer(ControlCharSafeSerializer):
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


class OfflineSaleInputSerializer(ControlCharSafeSerializer):
    client_sale_id = serializers.UUIDField()
    device_sequence = serializers.IntegerField(min_value=1)
    offline_created_at = serializers.DateTimeField()
    items = _OfflineItemSerializer(many=True)
    payments = _OfflinePaymentSerializer(many=True)
    customer_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=150
    )
    customer_phone = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=30
    )

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("At least one payment is required.")
        return value


class OfflineSyncRequestSerializer(ControlCharSafeSerializer):
    authorization_token = serializers.CharField(max_length=20000)
    sales = OfflineSaleInputSerializer(many=True)

    def validate_sales(self, value):
        if not value:
            raise serializers.ValidationError("Submit at least one sale.")
        return value


class OfflineTemporaryReceiptRequestSerializer(ControlCharSafeSerializer):
    authorization_token = serializers.CharField(max_length=20000)
    sale = OfflineSaleInputSerializer()


class OfflineSyncResultSerializer(ControlCharSafeSerializer):
    client_sale_id = serializers.CharField()
    device_sequence = serializers.IntegerField()
    outcome = serializers.CharField()
    detail_code = serializers.CharField(allow_blank=True)
    sale_id = serializers.CharField(allow_null=True)
    receipt_number = serializers.CharField(allow_null=True)


class OfflineSyncRecordSerializer(ControlCharSafeModelSerializer):
    official_receipt_number = serializers.CharField(
        source="sale.receipt_number", read_only=True, default=None
    )

    class Meta:
        model = OfflineSaleSyncRecord
        fields = [
            "id",
            "client_sale_id",
            "device_sequence",
            "offline_created_at",
            "outcome",
            "detail_code",
            "redacted_payload",
            "sale",
            "official_receipt_number",
            "resolved",
            "resolved_at",
            "resolution_note",
            "created_at",
        ]
        read_only_fields = fields


class OfflineSyncRecordResolveSerializer(ControlCharSafeSerializer):
    note = serializers.CharField(min_length=5, max_length=500)
