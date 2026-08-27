from django.contrib import admin

from apps.sales.models import Customer, Payment, ReceiptSequence, Sale, SaleItem


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "branch", "created_at")
    list_filter = ("branch",)
    search_fields = ("name", "phone")


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    readonly_fields = [f.name for f in SaleItem._meta.fields]
    can_delete = False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = [f.name for f in Payment._meta.fields]
    can_delete = False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = (
        "receipt_number",
        "branch",
        "cashier",
        "status",
        "total",
        "completed_at",
    )
    list_filter = ("branch", "status", "source")
    search_fields = ("receipt_number", "client_sale_id")
    inlines = [SaleItemInline, PaymentInline]
    readonly_fields = [f.name for f in Sale._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReceiptSequence)
class ReceiptSequenceAdmin(admin.ModelAdmin):
    list_display = ("branch", "business_date", "last_number")
    list_filter = ("branch",)
