"""Stage 16 — offline authorization lifecycle and the exclusive session."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import (
    DeviceStatus,
    OfflineAuthorizationStatus,
    RegisteredDevice,
)
from apps.accounts.tests.factories import EmployeeFactory, RegisteredDeviceFactory
from apps.core.exceptions import APIError, Conflict
from apps.sales.models import OfflineSaleSyncRecord, OfflineSyncOutcome
from apps.sales.services.offline import (
    end_offline_session,
    issue_offline_authorization,
    replace_offline_authorization,
    revoke_offline_authorization,
)
from apps.sales.services.offline_snapshot import verify_authorization_token
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


def _device(branch):
    return RegisteredDevice.objects.filter(
        branch=branch, status=DeviceStatus.ACTIVE
    ).first() or RegisteredDeviceFactory(branch=branch)


def _issue(branch, owner, *, cashier=None, device=None):
    return issue_offline_authorization(
        branch=branch,
        device=device or _device(branch),
        cashier=cashier or EmployeeFactory(branch=branch),
        owner=owner,
    )


class TestIssue:
    def test_owner_issues_a_bound_signed_24h_authorization(self, branch, owner):
        stocked_variant(branch, owner, price="1500.00", quantity=10, low_stock_level=4)
        auth = _issue(branch, owner)

        assert auth.status == OfflineAuthorizationStatus.ACTIVE
        assert auth.expires_at - auth.issued_at == timedelta(hours=24)
        assert auth.snapshot_version == 1

        decoded = verify_authorization_token(auth.signed_token)
        assert decoded["branch_id"] == str(branch.id)
        assert decoded["device_id"] == str(auth.device_id)
        assert decoded["authorization_id"] == str(auth.id)
        skus = {row["sku"] for row in decoded["snapshot"]["variants"]}
        assert len(skus) == 1

    def test_non_owner_cannot_issue(self, branch, owner):
        clerk = EmployeeFactory(branch=branch)
        with pytest.raises(APIError) as err:
            issue_offline_authorization(
                branch=branch,
                device=RegisteredDeviceFactory(branch=branch),
                cashier=clerk,
                owner=clerk,
            )
        assert err.value.code == "not_branch_owner"

    def test_one_active_session_per_branch(self, branch, owner):
        _issue(branch, owner, device=RegisteredDeviceFactory(branch=branch))
        with pytest.raises(Conflict) as err:
            _issue(branch, owner)
        assert err.value.code == "offline_session_active"

    def test_active_session_blocks_online_sales(self, branch, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        _issue(branch, owner)
        with pytest.raises(Conflict) as err:
            create_sale(
                branch=branch,
                cashier=EmployeeFactory(branch=branch),
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=[PaymentLine(method="CASH", amount=Decimal("1000.00"))],
                client_sale_id=uuid.uuid4(),
            )
        assert err.value.code == "offline_session_active"


class TestReplaceAndRevoke:
    def test_replace_closes_old_and_issues_a_new_version(self, branch, owner):
        stocked_variant(branch, owner, price="1000.00", quantity=5)
        first = _issue(branch, owner)
        second = replace_offline_authorization(authorization=first, owner=owner)
        first.refresh_from_db()
        assert first.status == OfflineAuthorizationStatus.REPLACED
        assert second.status == OfflineAuthorizationStatus.ACTIVE
        assert second.snapshot_version == first.snapshot_version + 1

    def test_revoke_marks_revoked(self, branch, owner):
        first = _issue(branch, owner)
        revoke_offline_authorization(
            authorization=first, owner=owner, reason="device lost"
        )
        first.refresh_from_db()
        assert first.status == OfflineAuthorizationStatus.REVOKED
        assert first.ended_by_id == owner.id
        assert first.needs_owner_review is True


class TestEndSession:
    def test_end_session_with_nothing_pending_reopens_online_sales(self, branch, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        auth = _issue(branch, owner)
        end_offline_session(authorization=auth, owner=owner)
        auth.refresh_from_db()
        assert auth.status == OfflineAuthorizationStatus.CLOSED
        # online sales work again
        sale = create_sale(
            branch=branch,
            cashier=EmployeeFactory(branch=branch),
            cart=[CartLine(variant_id=variant.id, quantity=1)],
            payments=[PaymentLine(method="CASH", amount=Decimal("1000.00"))],
            client_sale_id=uuid.uuid4(),
        )
        assert sale.status == "COMPLETED"

    def test_end_session_blocked_by_unresolved_record(self, branch, owner):
        auth = _issue(branch, owner)
        OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=auth,
            device=auth.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=1,
            offline_created_at=timezone.now(),
            outcome=OfflineSyncOutcome.CONFLICT,
            detail_code="stock_not_available",
        )
        with pytest.raises(Conflict) as err:
            end_offline_session(authorization=auth, owner=owner)
        assert err.value.code == "unresolved_offline_sales"

    def test_force_end_records_force_closed(self, branch, owner):
        auth = _issue(branch, owner)
        OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=auth,
            device=auth.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=1,
            offline_created_at=timezone.now(),
            outcome=OfflineSyncOutcome.OWNER_REVIEW_REQUIRED,
        )
        end_offline_session(
            authorization=auth, owner=owner, force=True, reason="closing shop"
        )
        auth.refresh_from_db()
        assert auth.status == OfflineAuthorizationStatus.FORCE_CLOSED
