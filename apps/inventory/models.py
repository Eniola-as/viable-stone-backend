"""Suppliers, restocks, per-branch balances and the immutable stock ledger."""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel, UUIDModel


class MovementType(models.TextChoices):
    OPENING = "OPENING", "Opening stock"
    RESTOCK = "RESTOCK", "Restock"
    SALE = "SALE", "Sale"
    RETURN = "RETURN", "Return"
    DAMAGE = "DAMAGE", "Damage / write-off"
    ADJUSTMENT = "ADJUSTMENT", "Correction"
    # Owner + MFA count correction applied while reconciling an offline sale
    # whose sync failed. NOT a purchase / restock / hidden stock creation —
    # it only rebuilds the pre-sale balance from a physical count so the
    # official sale's stock deduction lands on the counted quantity.
    OFFLINE_RECONCILIATION = "OFFLINE_RECONCILIATION", "Offline reconciliation"


class RestockStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    CONFIRMED = "CONFIRMED", "Confirmed"


class StockCountStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPLIED = "APPLIED", "Applied"
    CANCELLED = "CANCELLED", "Cancelled"


class Supplier(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="suppliers"
    )
    name = models.CharField(max_length=180)
    contact_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["branch", "is_active"])]

    def __str__(self):
        return self.name


class Restock(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="restocks"
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="restocks"
    )
    supplier_invoice_number = models.CharField(max_length=100, blank=True)
    invoice_file = models.FileField(upload_to="restocks/%Y/%m/", blank=True)
    date = models.DateField()
    status = models.CharField(
        max_length=10, choices=RestockStatus.choices, default=RestockStatus.DRAFT
    )
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="restocks_created",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="restocks_confirmed",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cost__gte=0), name="restock_total_cost_gte_0"
            ),
        ]

    def __str__(self):
        return f"Restock {self.pk} ({self.status})"

    @property
    def is_confirmed(self) -> bool:
        return self.status == RestockStatus.CONFIRMED


class RestockItem(UUIDModel):
    restock = models.ForeignKey(Restock, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="restock_items"
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["variant__sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["restock", "variant"], name="restockitem_unique_variant"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="restockitem_qty_gt_0"
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gt=0), name="restockitem_unit_cost_gt_0"
            ),
        ]


class InventoryBalance(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="inventory_balances"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="balances"
    )
    quantity = models.PositiveIntegerField(default=0)
    average_unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    last_restocked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["variant__sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "variant"],
                name="inventorybalance_unique_branch_variant",
            ),
            models.CheckConstraint(
                condition=models.Q(average_unit_cost__gte=0),
                name="inventorybalance_avg_cost_gte_0",
            ),
        ]

    def __str__(self):
        return f"{self.variant.sku}: {self.quantity}"


class StockMovement(UUIDModel):
    """Permanent, append-only ledger explaining every stock change."""

    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="stock_movements"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    movement_type = models.CharField(max_length=24, choices=MovementType.choices)
    quantity_delta = models.IntegerField()
    unit_cost_snapshot = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "variant", "-created_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(quantity_delta=0),
                name="stockmovement_delta_nonzero",
            ),
        ]

    def __str__(self):
        return f"{self.movement_type} {self.quantity_delta:+d} {self.variant_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("StockMovement rows are immutable and append-only.")
        super().save(*args, **kwargs)


class StockCount(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="stock_counts"
    )
    status = models.CharField(
        max_length=10,
        choices=StockCountStatus.choices,
        default=StockCountStatus.DRAFT,
    )
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="stock_counts_created",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_counts_applied",
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"StockCount {self.pk} ({self.status})"


class StockCountItem(UUIDModel):
    stock_count = models.ForeignKey(
        StockCount, on_delete=models.CASCADE, related_name="items"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="stock_count_items",
    )
    system_quantity_snapshot = models.PositiveIntegerField()
    counted_quantity = models.PositiveIntegerField()
    variance = models.IntegerField(default=0)

    class Meta:
        ordering = ["variant__sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["stock_count", "variant"],
                name="stockcountitem_unique_variant",
            ),
        ]

    def save(self, *args, **kwargs):
        self.variance = int(self.counted_quantity) - int(self.system_quantity_snapshot)
        super().save(*args, **kwargs)
