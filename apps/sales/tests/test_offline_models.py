"""Stage 16 — offline authorization / sync-record model constraints."""

import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import (
    DeviceStatus,
    OfflineAuthorizationStatus,
    OfflineDeviceAuthorization,
    RegisteredDevice,
)
from apps.accounts.tests.factories import (
    EmployeeFactory,
    OwnerFactory,
    RegisteredDeviceFactory,
)
from apps.sales.models import OfflineSaleSyncRecord, OfflineSyncOutcome

pytestmark = pytest.mark.django_db


def _authorization(branch, **over):
    now = timezone.now()
    device = (
        over.pop("device", None)
        or RegisteredDevice.objects.filter(
            branch=branch, status=DeviceStatus.ACTIVE
        ).first()
        or RegisteredDeviceFactory(branch=branch)
    )
    cashier = over.pop("cashier", None) or EmployeeFactory(branch=branch)
    owner = over.pop("authorized_by", None) or OwnerFactory(branch=branch)
    next_version = OfflineDeviceAuthorization.objects.filter(branch=branch).count() + 1
    defaults = {
        "branch": branch,
        "device": device,
        "cashier": cashier,
        "authorized_by": owner,
        "snapshot_version": next_version,
        "snapshot": {"version": 1, "variants": []},
        "signed_token": "signed.token.value",
        "issued_at": now,
        "expires_at": now + timedelta(hours=24),
        "status": OfflineAuthorizationStatus.ACTIVE,
    }
    defaults.update(over)
    return OfflineDeviceAuthorization.objects.create(**defaults)


class TestOfflineAuthorization:
    def test_only_one_active_authorization_per_branch(self, branch):
        _authorization(branch)
        with transaction.atomic(), pytest.raises(IntegrityError):
            _authorization(branch)

    def test_a_closed_authorization_frees_the_slot(self, branch):
        first = _authorization(branch)
        first.status = OfflineAuthorizationStatus.CLOSED
        first.save(update_fields=["status", "updated_at"])
        # a new ACTIVE one is now allowed
        second = _authorization(branch)
        assert second.pk != first.pk

    def test_snapshot_version_unique_per_branch(self, branch):
        first = _authorization(branch, snapshot_version=7)
        first.status = OfflineAuthorizationStatus.REPLACED
        first.save(update_fields=["status", "updated_at"])
        with transaction.atomic(), pytest.raises(IntegrityError):
            _authorization(branch, snapshot_version=7)

    def test_is_expired_and_needs_owner_review_helpers(self, branch):
        past = timezone.now() - timedelta(hours=1)
        auth = _authorization(
            branch, issued_at=past - timedelta(hours=24), expires_at=past
        )
        assert auth.is_expired is True
        # the helper is a pure status check — expiry is handled at sync time
        assert auth.needs_owner_review is False
        for status in (
            OfflineAuthorizationStatus.CLOSED,
            OfflineAuthorizationStatus.FORCE_CLOSED,
            OfflineAuthorizationStatus.REVOKED,
            OfflineAuthorizationStatus.REPLACED,
        ):
            auth.status = status
            assert auth.needs_owner_review is True


class TestOfflineSyncRecord:
    def test_unique_branch_client_sale_id(self, branch):
        auth = _authorization(branch)
        cid = uuid.uuid4()
        OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=auth,
            device=auth.device,
            client_sale_id=cid,
            device_sequence=1,
            offline_created_at=timezone.now(),
            outcome=OfflineSyncOutcome.ACCEPTED,
        )
        with transaction.atomic(), pytest.raises(IntegrityError):
            OfflineSaleSyncRecord.objects.create(
                branch=branch,
                authorization=auth,
                device=auth.device,
                client_sale_id=cid,
                device_sequence=2,
                offline_created_at=timezone.now(),
                outcome=OfflineSyncOutcome.DUPLICATE,
            )
