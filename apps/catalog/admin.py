from django.contrib import admin

from apps.catalog.models import Brand, Category, PriceHistory, Product, ProductVariant


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "is_active")
    list_filter = ("branch", "is_active")
    search_fields = ("name",)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "is_active")
    list_filter = ("branch", "is_active")
    search_fields = ("name",)


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "brand", "category", "branch", "is_active")
    list_filter = ("branch", "kind", "is_active", "category")
    search_fields = ("name",)
    inlines = [ProductVariantInline]


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("sku", "product", "colour", "size", "finish", "is_active")
    list_filter = ("is_active", "product__branch")
    search_fields = ("sku", "barcode", "product__name")


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("variant", "amount", "valid_from", "valid_to", "changed_by")
    list_filter = ("valid_from",)
    search_fields = ("variant__sku",)
    readonly_fields = ("variant", "amount", "valid_from", "valid_to", "changed_by")
