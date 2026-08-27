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
