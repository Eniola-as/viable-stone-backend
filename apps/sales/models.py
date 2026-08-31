"""Customers, receipt sequencing, sales, sale items, payments, returns."""

import uuid

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel, UUIDModel


class ApprovalType(models.TextChoices):
    DISCOUNT = "DISCOUNT", "Discount"
    RETURN = "RETURN", "Return"
    CORRECTION = "CORRECTION", "Correction"
    STOCK_ADJUSTMENT = "STOCK_ADJUSTMENT", "Stock adjustment"
    DAMAGE = "DAMAGE", "Damage"


class ApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class SaleReturnStatus(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    COMPLETED = "COMPLETED", "Completed"


class ReturnCondition(models.TextChoices):
    RESELLABLE = "RESELLABLE", "Resellable — restored to inventory"
    DAMAGED_OR_OPENED = "DAMAGED_OR_OPENED", "Damaged or opened — not restored"


class SaleStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    PENDING_SYNC = "PENDING_SYNC", "Pending sync"
    COMPLETED = "COMPLETED", "Completed"
    PARTIALLY_RETURNED = "PARTIALLY_RETURNED", "Partially returned"
    RETURNED = "RETURNED", "Returned"
    CANCELLED = "CANCELLED", "Cancelled"


class SaleSource(models.TextChoices):
    ONLINE = "ONLINE", "Online"
    OFFLINE = "OFFLINE", "Offline"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    TRANSFER = "TRANSFER", "Bank transfer"
    POS = "POS", "POS / card"


_FINALISED_STATUSES = frozenset(
    {
        SaleStatus.COMPLETED,
        SaleStatus.RETURNED,
        SaleStatus.PARTIALLY_RETURNED,
        SaleStatus.CANCELLED,
    }
)


class Customer(BaseModel):
    """Optional details for repeat or identified customers. Both fields optional."""

    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="customers"
    )
    name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["branch", "phone"])]

    def __str__(self):
        return self.name or self.phone or "Walk-in customer"


class ReceiptSequence(models.Model):
    """Per-branch, per-day counter. Locked while the next number is generated."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="receipt_sequences"
    )
    business_date = models.DateField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "business_date"],
                name="receiptsequence_unique_branch_date",
            ),
        ]

    def __str__(self):
        return f"{self.branch_id} {self.business_date}: {self.last_number}"


class Sale(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="sales"
    )
    receipt_number = models.CharField(max_length=80, null=True, blank=True)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales"
    )
    customer = models.ForeignKey(
        Customer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales",
    )
    status = models.CharField(
        max_length=20, choices=SaleStatus.choices, default=SaleStatus.DRAFT
    )
    source = models.CharField(
        max_length=10, choices=SaleSource.choices, default=SaleSource.ONLINE
    )
    client_sale_id = models.UUIDField()
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    change_due = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "client_sale_id"],
                name="sale_unique_branch_client_id",
            ),
            models.UniqueConstraint(
                fields=["receipt_number"],
                condition=models.Q(receipt_number__isnull=False),
                name="sale_unique_receipt_number",
            ),
            models.CheckConstraint(
                condition=models.Q(subtotal__gte=0)
                & models.Q(discount_total__gte=0)
                & models.Q(total__gte=0)
                & models.Q(change_due__gte=0),
                name="sale_money_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "status", "-created_at"]),
            models.Index(fields=["cashier", "-created_at"]),
        ]

    def __str__(self):
        return self.receipt_number or f"Sale {self.pk} ({self.status})"

    @property
    def is_completed(self) -> bool:
        return self.status == SaleStatus.COMPLETED

    def save(self, *args, **kwargs):
        # A finalised sale is immutable. Return/refund services that must move it
        # to a *_RETURNED state pass ``force=True`` explicitly.
        force = kwargs.pop("force", False)
        if not self._state.adding and not force:
            current = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if current in _FINALISED_STATUSES:
                raise ValueError(f"Sale {self.pk} is {current}; it cannot be modified.")
        super().save(*args, **kwargs)


class SaleItem(UUIDModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="sale_items"
    )
    product_name_snapshot = models.CharField(max_length=250)
    sku_snapshot = models.CharField(max_length=60)
    variant_description_snapshot = models.CharField(max_length=250, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price_snapshot = models.DecimalField(max_digits=14, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["sku_snapshot"]
        constraints = [
            models.UniqueConstraint(
                fields=["sale", "variant"], name="saleitem_unique_variant_per_sale"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="saleitem_qty_gt_0"
            ),
        ]


class Payment(UUIDModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    tendered_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    reference = models.CharField(max_length=150, blank=True)
    # True for offline-synced payments: physically confirmed by the cashier at
    # the till, never electronically verified by this system.
    offline_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payment_amount_gt_0"
            ),
        ]

    def __str__(self):
        return f"{self.method} {self.amount}"


# --------------------------------------------------------------------------- #
# Approvals and rare returns                                                  #
# --------------------------------------------------------------------------- #


class ApprovalRequest(BaseModel):
    """Generic request → owner decision. Approved / rejected rows are immutable."""

    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="approval_requests"
    )
    sale = models.ForeignKey(
        Sale,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approval_requests",
    )
    stock_count = models.ForeignKey(
        "inventory.StockCount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approval_requests",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approval_requests",
    )
    request_type = models.CharField(max_length=20, choices=ApprovalType.choices)
    status = models.CharField(
        max_length=10, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approval_requests_made",
    )
    reason = models.TextField()
    requested_changes = models.JSONField(default=dict, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approval_requests_reviewed",
    )
    reviewer_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status", "-created_at"]),
            models.Index(fields=["request_type", "status"]),
        ]

    def __str__(self):
        return f"{self.request_type} {self.status} ({self.pk})"

    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING


class SaleReturn(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="sale_returns"
    )
    original_sale = models.ForeignKey(
        Sale, on_delete=models.PROTECT, related_name="returns"
    )
    approval = models.OneToOneField(
        ApprovalRequest, on_delete=models.PROTECT, related_name="sale_return"
    )
    status = models.CharField(
        max_length=10,
        choices=SaleReturnStatus.choices,
        default=SaleReturnStatus.COMPLETED,
    )
    total = models.DecimalField(max_digits=14, decimal_places=2)
    client_return_id = models.UUIDField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sale_returns_approved",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "client_return_id"],
                name="salereturn_unique_branch_client_id",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0), name="salereturn_total_gte_0"
            ),
        ]

    def __str__(self):
        return f"Return {self.pk} for {self.original_sale_id}"


class SaleReturnItem(UUIDModel):
    sale_return = models.ForeignKey(
        SaleReturn, on_delete=models.CASCADE, related_name="items"
    )
    original_sale_item = models.ForeignKey(
        SaleItem, on_delete=models.PROTECT, related_name="return_items"
    )
    quantity = models.PositiveIntegerField()
    condition = models.CharField(max_length=20, choices=ReturnCondition.choices)
    unit_price_snapshot = models.DecimalField(max_digits=14, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["original_sale_item__sku_snapshot"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="salereturnitem_qty_gt_0"
            ),
        ]


class Refund(UUIDModel):
    sale_return = models.ForeignKey(
        SaleReturn, on_delete=models.CASCADE, related_name="refunds"
    )
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=150, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="refunds_issued",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="refund_amount_gt_0"
            ),
        ]

    def __str__(self):
        return f"Refund {self.method} {self.amount}"


# --------------------------------------------------------------------------- #
# Offline fixed-price checkout — synchronisation ledger                       #
# --------------------------------------------------------------------------- #


class OfflineSyncOutcome(models.TextChoices):
    ACCEPTED = "ACCEPTED", "Accepted — official Sale created"
    DUPLICATE = "DUPLICATE", "Duplicate — already synced"
    CONFLICT = "CONFLICT", "Conflict — retained for owner resolution"
    REJECTED = "REJECTED", "Rejected — invalid payload"
    OWNER_REVIEW_REQUIRED = "OWNER_REVIEW_REQUIRED", "Owner review required"


class OfflineSaleSyncRecord(BaseModel):
    """One row per submitted offline sale — the idempotency and audit ledger.

    ``(branch, client_sale_id)`` is unique: it is the database backstop that
    makes batch retries safe and lets conflicts be retained rather than
    discarded. ``redacted_payload`` keeps enough for an owner to resolve a
    conflict but never a payment reference or a customer phone number.
    """

    branch = models.ForeignKey(
        "accounts.Branch",
        on_delete=models.PROTECT,
        related_name="offline_sync_records",
    )
    authorization = models.ForeignKey(
        "accounts.OfflineDeviceAuthorization",
        on_delete=models.PROTECT,
        related_name="sync_records",
    )
    device = models.ForeignKey(
        "accounts.RegisteredDevice",
        on_delete=models.PROTECT,
        related_name="offline_sync_records",
    )
    client_sale_id = models.UUIDField()
    device_sequence = models.PositiveIntegerField()
    offline_created_at = models.DateTimeField()
    outcome = models.CharField(max_length=24, choices=OfflineSyncOutcome.choices)
    detail_code = models.CharField(max_length=60, blank=True)
    redacted_payload = models.JSONField(default=dict, blank=True)
    sale = models.OneToOneField(
        Sale,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="offline_sync_record",
    )
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="offline_sync_records_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["device_sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "client_sale_id"],
                name="offlinesyncrecord_unique_branch_client_id",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "outcome"]),
            models.Index(fields=["authorization", "device_sequence"]),
        ]

    def __str__(self):
        return f"OfflineSync {self.client_sale_id} ({self.outcome})"


# --------------------------------------------------------------------------- #
# Offline sale reconciliation — safe owner resolution of accounting-impacting #
# CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED sync records                     #
# --------------------------------------------------------------------------- #


class OfflineReconciliationKind(models.TextChoices):
    RECORDED_AS_SALE = "RECORDED_AS_SALE", "Recorded as an official sale"
    REFUNDED_AND_RETURNED = (
        "REFUNDED_AND_RETURNED",
        "Refunded — all goods returned, no sale",
    )
    LINKED_EXISTING_SALE = (
        "LINKED_EXISTING_SALE",
        "Linked to an owner-entered official sale",
    )


class OfflineAmountSource(models.TextChoices):
    """How the money amount recorded on a reconciliation was established.

    * ``SNAPSHOT_VERIFIED`` — RECORDED_AS_SALE / LINKED_EXISTING_SALE only: an
      amount recomputed as the sum of price times quantity from the
      cryptographically verified frozen catalogue snapshot. It is the
      authoritative *sale* total, not a statement about how much cash actually
      changed hands.
    * ``RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`` — REFUNDED_AND_RETURNED only:
      the retained device payment total parsed cleanly **and equals** the
      verified snapshot total, so the amount actually collected from the
      customer is known with confidence and is that agreed figure.
    * ``OWNER_ATTESTED`` — the amount could not be established from trusted
      retained data (broken signature / binding, malformed items, or retained
      payments that disagree with the verified snapshot). The owner attested the
      amount **actually collected** from physical evidence; it is never
      presented as cryptographically verified.
    """

    SNAPSHOT_VERIFIED = (
        "SNAPSHOT_VERIFIED",
        "Sale total recomputed from the cryptographically verified frozen snapshot",
    )
    RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT = (
        "RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT",
        "Retained device payments parsed cleanly and equal the verified snapshot total",
    )
    OWNER_ATTESTED = (
        "OWNER_ATTESTED",
        "Owner-attested amount collected (retained data could not establish it)",
    )


class OfflineLinkVerification(models.TextChoices):
    """How thoroughly a LINKED_EXISTING_SALE target was matched against the
    retained offline record, across three dimensions:

    1. line items and their quantities,
    2. payment methods and amounts,
    3. the transaction total.
    """

    FULL = "FULL", "All three dimensions were comparable and every one matched"
    PARTIAL = (
        "PARTIAL",
        "Every comparable dimension matched; fewer than three could be compared",
    )
    MANUAL_ATTESTED = (
        "MANUAL_ATTESTED",
        "No dimension was comparable — owner-attested manual match",
    )


class OfflineSaleReconciliation(BaseModel):
    """One immutable owner reconciliation of an accounting-impacting offline
    sync record. Creating this row is what marks the sync record ``resolved``;
    it is never edited afterward.
    """

    sync_record = models.OneToOneField(
        OfflineSaleSyncRecord,
        on_delete=models.PROTECT,
        related_name="reconciliation",
    )
    branch = models.ForeignKey(
        "accounts.Branch",
        on_delete=models.PROTECT,
        related_name="offline_reconciliations",
    )
    kind = models.CharField(max_length=24, choices=OfflineReconciliationKind.choices)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="offline_reconciliations",
    )
    resolved_at = models.DateTimeField()
    explanation = models.TextField()
    # RECORDED_AS_SALE / LINKED_EXISTING_SALE -> the official Sale.
    sale = models.OneToOneField(
        Sale,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="offline_reconciliation",
    )
    receipt_number = models.CharField(max_length=40, blank=True)
    # True only for LINKED_EXISTING_SALE (owner re-entered from trusted evidence
    # because the retained offline data could not be verified).
    linked_manually = models.BooleanField(default=False)
    # REFUNDED_AND_RETURNED: the amount actually collected from the customer, and
    # therefore the exact amount the refund evidence must total. NOT necessarily
    # the catalogue total — see ``amount_source``.
    refund_total = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    # How the money amount on this row was established (see OfflineAmountSource).
    amount_source = models.CharField(
        max_length=40, choices=OfflineAmountSource.choices, blank=True
    )
    # LINKED_EXISTING_SALE only: how thoroughly the linked sale was matched
    # against the retained offline record.
    link_verification = models.CharField(
        max_length=16, choices=OfflineLinkVerification.choices, blank=True
    )
    # Owner-only diagnostics for a REFUNDED_AND_RETURNED where the collected
    # amount was not simply the agreed figure: the verified catalogue total and
    # the retained device payment total, kept side by side so an owner can see
    # why an attestation was required. Never surfaced to cashiers or written to
    # audit-log rows.
    verified_snapshot_total = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    retained_payments_total = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    # True when the reconciliation had to substitute ``timezone.now()`` because
    # the retained ``offline_created_at`` was outside the authorised window.
    completion_time_substituted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-resolved_at"]

    def __str__(self):
        return f"OfflineReconciliation {self.kind} ({self.sync_record_id})"


class OfflineReconciliationRefund(UUIDModel):
    """Refund evidence for a REFUNDED_AND_RETURNED reconciliation.

    No ``Payment``, ``Refund`` or ``SaleReturn`` row is ever created for a
    fully-reversed offline sale that never entered the ledger — this is the
    only durable record of the reversal.
    """

    reconciliation = models.ForeignKey(
        OfflineSaleReconciliation,
        on_delete=models.CASCADE,
        related_name="refunds",
    )
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="offlinereconrefund_amount_gt_0",
            ),
        ]

    def __str__(self):
        return f"OfflineReconRefund {self.method} {self.amount}"


class OfflineReconciliationCount(UUIDModel):
    """Physical-count evidence for a stock-conflict RECORDED_AS_SALE.

    ``counted_on_hand`` is what the owner physically counted on the shelf *now*,
    after the offline goods have already left. One row per affected variant.
    """

    reconciliation = models.ForeignKey(
        OfflineSaleReconciliation,
        on_delete=models.CASCADE,
        related_name="counts",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="+",
    )
    counted_on_hand = models.PositiveIntegerField()
    quantity_in_offline_sale = models.PositiveIntegerField()
    db_quantity_before = models.IntegerField()
    # reconstructed_pre_sale - db_quantity_before  (the OFFLINE_RECONCILIATION
    # correction applied before the sale deduction; may be positive or negative).
    correction_delta = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["reconciliation", "variant"],
                name="offlinereconcount_unique_variant",
            ),
        ]

    def __str__(self):
        return f"OfflineReconCount {self.variant_id} -> {self.counted_on_hand}"
