from django.contrib import admin

from apps.inventory.models import (
    InventoryBalance,
    Restock,
    RestockItem,
    StockCount,
    StockCountItem,
    StockMovement,
    Supplier,
)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "phone", "is_active")
    list_filter = ("branch", "is_active")
    search_fields = ("name", "contact_name", "phone")


class RestockItemInline(admin.TabularInline):
    model = RestockItem
    extra = 0
    readonly_fields = ("line_total",)


@admin.register(Restock)
class RestockAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "supplier", "date", "status", "total_cost")
    list_filter = ("branch", "status")
    inlines = [RestockItemInline]
    readonly_fields = ("total_cost", "confirmed_by", "confirmed_at")


@admin.register(InventoryBalance)
class InventoryBalanceAdmin(admin.ModelAdmin):
    list_display = ("variant", "branch", "quantity", "average_unit_cost")
    list_filter = ("branch",)
    search_fields = ("variant__sku",)
    readonly_fields = ("quantity", "average_unit_cost", "last_restocked_at")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "variant",
        "movement_type",
        "quantity_delta",
        "reference_type",
    )
    list_filter = ("movement_type", "branch")
    search_fields = ("variant__sku", "reference_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class StockCountItemInline(admin.TabularInline):
    model = StockCountItem
    extra = 0
    readonly_fields = ("variance",)


@admin.register(StockCount)
class StockCountAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "status", "created_by", "applied_at")
    list_filter = ("branch", "status")
    inlines = [StockCountItemInline]
