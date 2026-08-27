"""Shared abstract models and the append-only audit log."""

import uuid

from django.conf import settings
from django.db import models


class UUIDModel(models.Model):
    """Primary key as a non-sequential UUID."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BaseModel(UUIDModel):
    """UUID id plus creation/update timestamps for every business table."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class AuditLog(UUIDModel):
    """Append-only record of sensitive actions.

    Never store passwords, cookies, recovery codes, TOTP secrets, full customer
    phone numbers or uploaded-file contents in ``before`` / ``after``.
    """

    branch = models.ForeignKey(
        "accounts.Branch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["action"]),
            models.Index(fields=["branch", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.target_type}:{self.target_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("AuditLog rows are immutable and append-only.")
        super().save(*args, **kwargs)
