"""Expense categories and expenses. Reports are computed, never stored."""

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import BaseModel


class ExpenseCategory(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="expense_categories"
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"
        constraints = [
            models.UniqueConstraint(
                "branch", Lower("name"), name="expensecategory_branch_name_ci_unique"
            ),
        ]

    def __str__(self):
        return self.name


class Expense(BaseModel):
    branch = models.ForeignKey(
        "accounts.Branch", on_delete=models.PROTECT, related_name="expenses"
    )
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    expense_date = models.DateField()
    description = models.TextField()
    receipt_file = models.FileField(upload_to="expenses/%Y/%m/", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="expenses_created",
    )
    is_voided = models.BooleanField(default=False)
    void_reason = models.TextField(blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses_voided",
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        indexes = [
            models.Index(fields=["branch", "expense_date"]),
            models.Index(fields=["branch", "is_voided"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="expense_amount_gt_0"
            ),
        ]

    def __str__(self):
        return f"{self.category_id} {self.amount} ({self.expense_date})"
