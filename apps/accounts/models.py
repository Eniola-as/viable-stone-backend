"""Branch, custom User, MFA recovery codes and the single offline device."""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import BaseModel, UUIDModel


class Role(models.TextChoices):
    TECH_ADMIN = "TECH_ADMIN", "Technical administrator"
    OWNER = "OWNER", "Owner"
    EMPLOYEE = "EMPLOYEE", "Employee"


MFA_ROLES = frozenset({Role.TECH_ADMIN, Role.OWNER})


class Branch(BaseModel):
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=50, default="Africa/Lagos")
    receipt_prefix = models.CharField(max_length=20, default="VS")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "branches"
        constraints = [
            models.UniqueConstraint(
                Lower("code"),
                name="branch_code_ci_unique",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", Role.TECH_ADMIN)
        extra_fields.setdefault("mfa_required", True)
        extra_fields.setdefault("must_change_password", False)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """Custom user.

    One individual account per employee (no shared logins); accounts with
    business history are deactivated, never deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.EMPLOYEE)
    branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
    )
    phone = models.CharField(max_length=30, blank=True)
    must_change_password = models.BooleanField(default=True)
    mfa_required = models.BooleanField(default=False)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    class Meta:
        ordering = ["username"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=[r.value for r in Role]),
                name="user_role_valid",
            ),
            # Owners and employees are always attached to a branch;
            # the technical administrator may be branchless.
            models.CheckConstraint(
                condition=models.Q(role=Role.TECH_ADMIN)
                | models.Q(branch__isnull=False),
                name="user_branch_required_unless_tech_admin",
            ),
        ]

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER

    @property
    def is_tech_admin(self) -> bool:
        return self.role == Role.TECH_ADMIN

    @property
    def is_employee(self) -> bool:
        return self.role == Role.EMPLOYEE

    def clean(self):
        super().clean()
        if self.role != Role.TECH_ADMIN and self.branch_id is None:
            raise ValidationError({"branch": "This role must belong to a branch."})

    def save(self, *args, **kwargs):
        if self.role in MFA_ROLES:
            self.mfa_required = True
        super().save(*args, **kwargs)


class RecoveryCode(UUIDModel):
    """One-use MFA recovery code. Only the hash is ever stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recovery_codes",
    )
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "code_hash"], name="recoverycode_user_hash_unique"
            ),
        ]

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


class DeviceStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REVOKED = "REVOKED", "Revoked"
    REPLACED = "REPLACED", "Replaced"


class RegisteredDevice(BaseModel):
    """The single device approved for fixed-price offline checkout per branch."""

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="devices")
    device_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=10, choices=DeviceStatus.choices, default=DeviceStatus.ACTIVE
    )
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="registered_devices",
    )
    registered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-registered_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch"],
                condition=models.Q(status=DeviceStatus.ACTIVE),
                name="one_active_offline_device_per_branch",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"


class OfflineAuthorizationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active — offline session running"
    CLOSED = "CLOSED", "Closed — session ended, all sales synced"
    FORCE_CLOSED = "FORCE_CLOSED", "Force-closed by owner with pending sales"
    REVOKED = "REVOKED", "Revoked"
    REPLACED = "REPLACED", "Replaced by a newer authorization"


# Statuses whose pending offline sales must not sync automatically; the owner
# has to review them explicitly.
OFFLINE_REVIEW_STATUSES = frozenset(
    {
        OfflineAuthorizationStatus.FORCE_CLOSED,
        OfflineAuthorizationStatus.REVOKED,
        OfflineAuthorizationStatus.REPLACED,
    }
)


class OfflineDeviceAuthorization(BaseModel):
    """One offline checkout session grant, bound to a single device.

    Binds the device, branch, cashier, a versioned signed catalogue snapshot
    and a validity window (at most ``settings.OFFLINE_AUTHORIZATION_MAX_HOURS``).
    The ``snapshot`` JSON never carries costs, credentials, signing secrets or
    customer information.
    """

    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="offline_authorizations"
    )
    device = models.ForeignKey(
        RegisteredDevice,
        on_delete=models.PROTECT,
        related_name="offline_authorizations",
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="offline_authorizations_as_cashier",
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="offline_authorizations_granted",
    )
    snapshot_version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    signed_token = models.TextField()
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=OfflineAuthorizationStatus.choices,
        default=OfflineAuthorizationStatus.ACTIVE,
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="offline_authorizations_ended",
    )
    end_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch"],
                condition=models.Q(status=OfflineAuthorizationStatus.ACTIVE),
                name="one_active_offline_authorization_per_branch",
            ),
            models.UniqueConstraint(
                fields=["branch", "snapshot_version"],
                name="offlineauth_unique_branch_version",
            ),
        ]
        indexes = [models.Index(fields=["branch", "status"])]

    def __str__(self):
        return f"OfflineAuth {self.pk} ({self.status})"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return timezone.now() > self.expires_at

    @property
    def needs_owner_review(self) -> bool:
        return self.status in OFFLINE_REVIEW_STATUSES
