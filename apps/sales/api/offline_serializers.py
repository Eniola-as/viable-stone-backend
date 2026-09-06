from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import OfflineDeviceAuthorization, RegisteredDevice
from apps.core.serializers import (
    ControlCharSafeModelSerializer,
    ControlCharSafeSerializer,
)
from apps.sales.models import (
    OfflineReconciliationCount,
    OfflineReconciliationKind,
    OfflineReconciliationRefund,
    OfflineSaleReconciliation,
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    PaymentMethod,
)

# Every ``detail_code`` sync can attach to a per-sale result / record, grouped
# by the ``outcome`` it accompanies. ACCEPTED / DUPLICATE carry ``""``.
OFFLINE_SYNC_DETAIL_CODES_DOC = (
    'detail_code accompanies the outcome; it is "" for ACCEPTED / DUPLICATE.\n'
    "REJECTED (invalid payload — the device already took real money, so the "
    "owner must reconcile it manually): duplicate_sequence, outside_window, "
    "invalid_timestamp, empty_cart, price_not_in_snapshot, invalid_quantity, "
    "payment_required, invalid_payment_method, invalid_payment_amount, "
    "reference_required, invalid_tendered_amount, payment_mismatch, "
    "variant_not_found, variant_unavailable.\n"
    "CONFLICT (a real sale that cannot be applied now — retained for the "
    "owner): stock_not_available, sale_in_progress.\n"
    "OWNER_REVIEW_REQUIRED (the offline session was over — ended, force-ended, "
    "revoked, replaced, or its window had lapsed — when this sale synced): "
    "closed, force_closed, revoked, replaced, expired."
)


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


class OfflineCatalogueSnapshotItemSerializer(ControlCharSafeSerializer):
    """One active, priced variant, frozen when the offline session opened.

    Selling data only — the reader is a cashier, never a branch owner.
    """

    variant_id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    sku = serializers.CharField(read_only=True)
    # Exact selling price as a Decimal string (2 dp) — never a float.
    price = serializers.CharField(read_only=True)
    # Whole units available at session start (snapshot info, not live stock).
    quantity = serializers.IntegerField(read_only=True)
    low_stock_level = serializers.IntegerField(read_only=True)


class OfflineCatalogueSnapshotSerializer(ControlCharSafeSerializer):
    """The signed fixed-price catalogue for the bound cashier's active session.

    ``status`` is always ``ACTIVE`` for a 200 response; a dead session returns
    the standard error envelope. ``signed_token`` is the same value returned by
    ``POST /offline/authorizations/`` — pass it back to ``/offline/sync/`` and
    ``/offline/temporary-receipt/`` as ``authorization_token``.
    """

    authorization_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    snapshot_version = serializers.IntegerField(read_only=True)
    issued_at = serializers.DateTimeField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    signed_token = serializers.CharField(read_only=True)
    items = OfflineCatalogueSnapshotItemSerializer(many=True, read_only=True)


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
    outcome = serializers.ChoiceField(choices=OfflineSyncOutcome.choices)
    detail_code = serializers.CharField(
        allow_blank=True, help_text=OFFLINE_SYNC_DETAIL_CODES_DOC
    )
    sale_id = serializers.CharField(allow_null=True)
    receipt_number = serializers.CharField(allow_null=True)


class OfflineSyncResponseSerializer(ControlCharSafeSerializer):
    """G23 — ``POST /offline/sync/`` returns one object, not a bare array.

    ``results`` holds exactly one entry per submitted sale, in device-sequence
    order.
    """

    results = OfflineSyncResultSerializer(many=True)


class OfflineSaleLookupSerializer(ControlCharSafeSerializer):
    """``GET /offline/sales/{client_sale_id}/`` — one submitted offline sale.

    Carries the sync-record's outcome **and** resolution state so the bound
    cashier's device can clear a "needs the owner" row even when the owner
    resolved it without creating an official Sale (a resolved CONFLICT, or any
    REJECTED). No cost, token or customer data.
    """

    client_sale_id = serializers.UUIDField(read_only=True)
    device_sequence = serializers.IntegerField(read_only=True)
    outcome = serializers.ChoiceField(
        choices=OfflineSyncOutcome.choices, read_only=True
    )
    detail_code = serializers.CharField(
        read_only=True, allow_blank=True, help_text=OFFLINE_SYNC_DETAIL_CODES_DOC
    )
    sale_id = serializers.UUIDField(read_only=True, allow_null=True)
    receipt_number = serializers.CharField(read_only=True, allow_null=True)
    resolved = serializers.BooleanField(read_only=True)
    resolved_at = serializers.DateTimeField(read_only=True, allow_null=True)
    resolution_note = serializers.CharField(read_only=True, allow_blank=True)
    # Safe status field so the cashier's device can pick the right message.
    # null until the owner reconciles; never carries amounts / cost.
    resolution_kind = serializers.ChoiceField(
        choices=OfflineReconciliationKind.choices, read_only=True, allow_null=True
    )


class _RetainedPaymentSerializer(ControlCharSafeSerializer):
    """One retained offline payment line — the owner uses ``payment_index`` to
    associate a Transfer/POS reference on RECORDED_AS_SALE. The device's
    original reference was never stored; ``method`` + ``amount`` only."""

    payment_index = serializers.IntegerField(read_only=True)
    method = serializers.ChoiceField(choices=PaymentMethod.choices, read_only=True)
    amount = serializers.CharField(read_only=True)
    reference_required = serializers.BooleanField(read_only=True)


class OfflineSyncRecordSerializer(ControlCharSafeModelSerializer):
    official_receipt_number = serializers.CharField(
        source="sale.receipt_number", read_only=True, default=None
    )
    # Owner-only view (this viewset is IsOwner): a typed projection of the
    # retained payments so the owner can supply the correct reference per
    # ``payment_index`` on RECORDED_AS_SALE. No reference is exposed (none is
    # stored).
    retained_payments = serializers.SerializerMethodField()

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
            "retained_payments",
            "sale",
            "official_receipt_number",
            "resolved",
            "resolved_at",
            "resolution_note",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(_RetainedPaymentSerializer(many=True))
    def get_retained_payments(self, obj):
        from apps.sales.services.offline_reconciliation import _retained_payments

        rows = _retained_payments(obj) or []
        return [
            {
                "payment_index": r["payment_index"],
                "method": r["method"],
                "amount": str(r["amount"]),
                "reference_required": r["reference_required"],
            }
            for r in rows
        ]


class OfflineSyncRecordDetailSerializer(OfflineSyncRecordSerializer):
    """Single-record read (owner + MFA). Adds the two pre-confirm diagnostics
    so the owner can see catalogue-vs-collected divergence **before** attesting
    a ``REFUNDED_AND_RETURNED`` — the same figures the reconciliation result
    carries afterwards.

    Kept off the list response: each is an HMAC verify of the retained signed
    token / a payload parse, wasteful per row. ``verified_snapshot_total`` is
    ``null`` when the token cannot be verified or an item is not in the signed
    snapshot; ``retained_payments_total`` is ``null`` when the retained payments
    are absent or unparseable.
    """

    verified_snapshot_total = serializers.SerializerMethodField()
    retained_payments_total = serializers.SerializerMethodField()

    class Meta(OfflineSyncRecordSerializer.Meta):
        fields = [
            *OfflineSyncRecordSerializer.Meta.fields,
            "verified_snapshot_total",
            "retained_payments_total",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_verified_snapshot_total(self, obj):
        from apps.sales.services.offline_reconciliation import _verified_offline_total

        value = _verified_offline_total(obj)
        return None if value is None else str(value)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_retained_payments_total(self, obj):
        from apps.sales.services.offline_reconciliation import _retained_payments_total

        value = _retained_payments_total(obj)
        return None if value is None else str(value)


class OfflineSyncRecordResolveSerializer(ControlCharSafeSerializer):
    note = serializers.CharField(min_length=5, max_length=500)


# --------------------------------------------------------------------------- #
# Safe reconciliation of accounting-impacting sync records                    #
# --------------------------------------------------------------------------- #


class _ReconcileCountSerializer(ControlCharSafeSerializer):
    variant = serializers.UUIDField()
    # min not enforced here so a negative maps to the stable ``invalid_count``.
    counted_on_hand = serializers.IntegerField()


class _ReconcilePaymentReferenceSerializer(ControlCharSafeSerializer):
    payment_index = serializers.IntegerField(min_value=0)
    reference = serializers.CharField(allow_blank=True, default="", max_length=150)


class _ReconcileRefundSerializer(ControlCharSafeSerializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reference = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=150
    )


class OfflineReconcileRequestSerializer(ControlCharSafeSerializer):
    """Owner reconciliation of a CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED
    record. Fields apply per ``kind``:

    * ``RECORDED_AS_SALE`` — ``explanation``; ``counts`` (one per affected
      variant — **required** when the record's outcome is ``CONFLICT`` or when
      stock is short); ``payment_references`` as
      ``[{payment_index, reference}]`` where ``payment_index`` is the 0-based
      position in the record's ``retained_payments`` (one entry per retained
      Transfer/POS payment). The backend reads the retained payload + verified
      signed snapshot; you never resend lines, prices, amounts or the customer.
    * ``REFUNDED_AND_RETURNED`` — ``explanation``; ``all_goods_returned`` and
      ``full_amount_refunded`` must both be ``true``; ``refunds`` must total the
      money **actually collected** from the customer (Transfer/POS refund needs
      a ``reference``). That amount is trusted automatically **only** when the
      retained device payments parse cleanly and equal the verified snapshot
      total (``amount_source: RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT``). If they
      disagree, or the retained data cannot be verified, set
      ``owner_attestation: true`` + ``attested_offline_total`` (the amount
      actually collected, from physical evidence) + a ≥40-char ``explanation``;
      the result is flagged ``amount_source: OWNER_ATTESTED``. A non-positive
      collected amount is rejected (``no_payment_to_refund``). No Sale /
      Payment / stock movement is created.
    * ``LINKED_EXISTING_SALE`` — fallback; ``explanation`` and one of
      ``sale_id`` / ``receipt_number`` for a COMPLETED sale in this branch. The
      backend compares three dimensions — (1) line items & quantities,
      (2) payment methods & amounts, (3) transaction total — against the
      retained record; any comparable dimension that mismatches ->
      ``sale_incompatible``. If **nothing** is comparable, set
      ``owner_attestation: true`` + a ≥40-char ``explanation``
      (``link_verification: MANUAL_ATTESTED``).
    """

    kind = serializers.ChoiceField(choices=OfflineReconciliationKind.choices)
    explanation = serializers.CharField(min_length=10, max_length=2000)

    counts = _ReconcileCountSerializer(many=True, required=False)
    payment_references = _ReconcilePaymentReferenceSerializer(many=True, required=False)

    all_goods_returned = serializers.BooleanField(required=False, default=False)
    full_amount_refunded = serializers.BooleanField(required=False, default=False)
    refunds = _ReconcileRefundSerializer(many=True, required=False)

    owner_attestation = serializers.BooleanField(required=False, default=False)
    attested_offline_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )

    sale_id = serializers.UUIDField(required=False)
    receipt_number = serializers.CharField(
        required=False, allow_blank=True, max_length=40
    )


class _OfflineReconRefundReadSerializer(ControlCharSafeModelSerializer):
    """Immutable refund evidence for a REFUNDED_AND_RETURNED reconciliation.

    ``reference`` is the value the **owner** typed at reconcile time (a bank /
    POS slip number). It is deliberately echoed back on this owner + MFA
    response so the owner can confirm what was recorded; it is **never** placed
    in an audit-log row and **never** reaches the cashier's
    ``OfflineSaleLookup`` (that read model has no ``refunds`` field at all).
    The device's *original* offline payment reference was discarded at sync
    time and is not stored anywhere.
    """

    class Meta:
        model = OfflineReconciliationRefund
        fields = ["method", "amount", "reference"]
        read_only_fields = fields


class _OfflineReconCountReadSerializer(ControlCharSafeModelSerializer):
    class Meta:
        model = OfflineReconciliationCount
        fields = [
            "variant",
            "counted_on_hand",
            "quantity_in_offline_sale",
            "db_quantity_before",
            "correction_delta",
        ]
        read_only_fields = fields


class OfflineReconciliationSerializer(ControlCharSafeModelSerializer):
    """Owner + MFA only (returned by the ``reconcile`` action).

    ``verified_snapshot_total`` and ``retained_payments_total`` are owner-only
    diagnostics for a REFUNDED_AND_RETURNED that needed an attestation — they
    show the verified catalogue total and the retained device payment total
    side by side. They are absent from the cashier ``OfflineSaleLookup`` and
    are never written to an audit-log row.
    """

    resolved_by_username = serializers.CharField(
        source="resolved_by.username", read_only=True
    )
    sale_id = serializers.UUIDField(read_only=True, allow_null=True)
    refunds = _OfflineReconRefundReadSerializer(many=True, read_only=True)
    counts = _OfflineReconCountReadSerializer(many=True, read_only=True)

    class Meta:
        model = OfflineSaleReconciliation
        fields = [
            "id",
            "kind",
            "resolved_by_username",
            "resolved_at",
            "explanation",
            "sale_id",
            "receipt_number",
            "refund_total",
            "amount_source",
            "link_verification",
            "linked_manually",
            "completion_time_substituted",
            "verified_snapshot_total",
            "retained_payments_total",
            "refunds",
            "counts",
            "created_at",
        ]
        read_only_fields = fields


class OfflineReconciliationResultSerializer(ControlCharSafeSerializer):
    sync_record = OfflineSyncRecordSerializer(read_only=True)
    reconciliation = OfflineReconciliationSerializer(read_only=True)
