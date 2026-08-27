from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Branch, RecoveryCode, RegisteredDevice, User


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "phone", "is_active", "created_at")
    search_fields = ("code", "name")
    list_filter = ("is_active",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "role", "branch", "is_active", "mfa_required")
    list_filter = ("role", "branch", "is_active", "mfa_required")
    fieldsets = (
        *DjangoUserAdmin.fieldsets,
        (
            "Shop profile",
            {
                "fields": (
                    "role",
                    "branch",
                    "phone",
                    "must_change_password",
                    "mfa_required",
                    "last_activity_at",
                )
            },
        ),
    )
    readonly_fields = ("last_activity_at",)


@admin.register(RegisteredDevice)
class RegisteredDeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "status", "registered_by", "registered_at")
    list_filter = ("status", "branch")


@admin.register(RecoveryCode)
class RecoveryCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "is_used", "created_at")
    list_filter = ("used_at",)
    readonly_fields = ("user", "code_hash", "used_at", "created_at")
