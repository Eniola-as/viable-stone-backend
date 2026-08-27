"""Catalogue: categories, brands, products, exact variants and price history."""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import BaseModel, UUIDModel


class ProductKind(models.TextChoices):
    PAINT = "PAINT", "Paint"
    EQUIPMENT = "EQUIPMENT", "Painting equipment"


class Category(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="categories"
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                "branch", Lower("name"), name="category_branch_name_ci_unique"
            ),
        ]

    def __str__(self):
        return self.name


class Brand(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="brands"
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "branch", Lower("name"), name="brand_branch_name_ci_unique"
            ),
        ]

    def __str__(self):
        return self.name


class Product(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="products"
    )
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    brand = models.ForeignKey(
        Brand,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="products",
    )
    kind = models.CharField(max_length=10, choices=ProductKind.choices)
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="products/%Y/%m/", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["branch", "kind"]),
            models.Index(fields=["branch", "is_active"]),
        ]

    def __str__(self):
        return self.name


class ProductVariant(BaseModel):
    """One exact sellable, stock-counted item (brand+colour+size+finish)."""

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="variants"
    )
    colour = models.CharField(max_length=100, blank=True)
    size = models.CharField(max_length=100, blank=True)
    finish = models.CharField(max_length=100, blank=True)
    sku = models.CharField(max_length=60, unique=True)
    barcode = models.CharField(max_length=100, blank=True, null=True)
    low_stock_level = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sku"]
        constraints = [
            models.UniqueConstraint(
                "product",
                Lower("colour"),
                Lower("size"),
                Lower("finish"),
                name="variant_attributes_unique_per_product",
            ),
            models.UniqueConstraint(
                Lower("barcode"),
                name="variant_barcode_ci_unique",
                condition=models.Q(barcode__isnull=False),
            ),
        ]

    def __str__(self):
        bits = [self.product.name, self.colour, self.size, self.finish]
        return " / ".join(b for b in bits if b)

    @property
    def description(self) -> str:
        return " / ".join(b for b in (self.colour, self.size, self.finish) if b)

    def save(self, *args, **kwargs):
        self.sku = self.sku.strip().upper()
        if self.barcode is not None:
            self.barcode = self.barcode.strip() or None
        super().save(*args, **kwargs)


class PriceHistory(UUIDModel):
    """Every approved selling price. Rows are never edited, only closed."""

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="price_history"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="price_changes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_from"]
        verbose_name_plural = "price history"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="price_amount_positive"
            ),
            models.UniqueConstraint(
                fields=["variant"],
                condition=models.Q(valid_to__isnull=True),
                name="one_current_price_per_variant",
            ),
        ]

    def __str__(self):
        return f"{self.variant.sku} @ {self.amount}"
