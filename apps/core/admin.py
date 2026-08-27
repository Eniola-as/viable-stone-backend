from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "action",
        "target_type",
        "target_id",
        "actor",
        "branch",
    )
    list_filter = ("action", "target_type")
    search_fields = ("target_id", "request_id", "action")
    readonly_fields = (
        "branch",
        "actor",
        "action",
        "target_type",
        "target_id",
        "request_id",
        "before",
        "after",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
