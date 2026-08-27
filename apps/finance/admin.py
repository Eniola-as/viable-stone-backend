from django.contrib import admin

from apps.finance.models import Expense, ExpenseCategory


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "is_active")
    list_filter = ("branch", "is_active")
    search_fields = ("name",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "expense_date",
        "category",
        "amount",
        "branch",
        "is_voided",
        "created_by",
    )
    list_filter = ("branch", "is_voided", "category")
    search_fields = ("description",)
    readonly_fields = (
        "amount",
        "created_by",
        "is_voided",
        "void_reason",
        "voided_by",
        "voided_at",
    )

    def has_delete_permission(self, request, obj=None):
        return False
