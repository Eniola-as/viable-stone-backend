import pytest
from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from django.db.utils import DataError

from apps.accounts.models import Branch, DeviceStatus, RecoveryCode, Role, User

from .factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
    RegisteredDeviceFactory,
    TechAdminFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


class TestBranch:
    def test_code_is_uppercased_and_trimmed_on_save(self):
        branch = Branch.objects.create(code="  vs09  ", name="Test")
        assert branch.code == "VS09"

    def test_code_is_case_insensitively_unique(self):
        BranchFactory(code="VS01")
        with pytest.raises(IntegrityError), transaction.atomic():
            Branch.objects.create(code="vs01", name="Duplicate")

    def test_defaults(self):
        branch = Branch.objects.create(code="VS02", name="Main")
        assert branch.timezone == "Africa/Lagos"
        assert branch.receipt_prefix == "VS"
        assert branch.is_active is True


class TestUserRolesAndBranch:
    def test_employee_requires_branch(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(username="no-branch", role=Role.EMPLOYEE, branch=None)

    def test_owner_requires_branch(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(
                username="owner-no-branch", role=Role.OWNER, branch=None
            )

    def test_tech_admin_may_be_branchless(self):
        user = User.objects.create(username="tech", role=Role.TECH_ADMIN, branch=None)
        assert user.branch_id is None
        assert user.is_tech_admin

    def test_invalid_role_rejected_by_check_constraint(self):
        branch = BranchFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(username="weird", role="SUPERBOSS", branch=branch)

    def test_mfa_required_forced_for_owner_and_tech_admin(self):
        owner = OwnerFactory(mfa_required=False)
        tech = TechAdminFactory(mfa_required=False)
        owner.refresh_from_db()
        tech.refresh_from_db()
        assert owner.mfa_required is True
        assert tech.mfa_required is True

    def test_mfa_not_forced_for_employee(self):
        employee = EmployeeFactory()
        assert employee.mfa_required is False

    def test_role_helper_properties_are_exclusive(self):
        owner = OwnerFactory()
        assert (owner.is_owner, owner.is_employee, owner.is_tech_admin) == (
            True,
            False,
            False,
        )

    def test_must_change_password_defaults_true(self):
        assert UserFactory().must_change_password is True

    def test_created_superuser_is_tech_admin(self):
        admin = User.objects.create_superuser(username="root", password="x-9f8g7h6j5k")
        assert admin.role == Role.TECH_ADMIN
        assert admin.mfa_required is True
        assert admin.is_superuser is True


class TestAuthentication:
    def test_active_user_authenticates(self):
        user = EmployeeFactory(username="cashier1", password="right-pass-123")
        assert authenticate(username="cashier1", password="right-pass-123") == user

    def test_inactive_user_cannot_authenticate(self):
        EmployeeFactory(username="ghost", password="right-pass-123", is_active=False)
        assert authenticate(username="ghost", password="right-pass-123") is None

    def test_wrong_password_fails(self):
        EmployeeFactory(username="cashier2", password="right-pass-123")
        assert authenticate(username="cashier2", password="nope") is None


class TestRecoveryCode:
    def test_same_hash_cannot_repeat_for_one_user(self):
        user = OwnerFactory()
        RecoveryCode.objects.create(user=user, code_hash="abc123")
        with pytest.raises(IntegrityError), transaction.atomic():
            RecoveryCode.objects.create(user=user, code_hash="abc123")

    def test_is_used_flag(self):
        from django.utils import timezone

        code = RecoveryCode.objects.create(user=OwnerFactory(), code_hash="z")
        assert code.is_used is False
        code.used_at = timezone.now()
        code.save()
        assert code.is_used is True


class TestRegisteredDevice:
    def test_only_one_active_device_per_branch(self):
        branch = BranchFactory()
        RegisteredDeviceFactory(branch=branch, status=DeviceStatus.ACTIVE)
        with pytest.raises(IntegrityError), transaction.atomic():
            RegisteredDeviceFactory(branch=branch, status=DeviceStatus.ACTIVE)

    def test_revoked_device_does_not_block_a_new_active_one(self):
        branch = BranchFactory()
        RegisteredDeviceFactory(branch=branch, status=DeviceStatus.REVOKED)
        active = RegisteredDeviceFactory(branch=branch, status=DeviceStatus.ACTIVE)
        assert active.status == DeviceStatus.ACTIVE

    def test_two_branches_can_each_have_an_active_device(self):
        RegisteredDeviceFactory(branch=BranchFactory(), status=DeviceStatus.ACTIVE)
        RegisteredDeviceFactory(branch=BranchFactory(), status=DeviceStatus.ACTIVE)


class TestAuditLogImmutable:
    def test_audit_log_cannot_be_updated(self):
        from apps.core.models import AuditLog

        entry = AuditLog.objects.create(action="test.action", target_type="Thing")
        entry.action = "changed"
        with pytest.raises(ValueError, match="append-only"):
            entry.save()


@pytest.mark.parametrize("bad_length", ["X" * 11])
def test_branch_code_max_length_enforced(bad_length):
    with pytest.raises((DataError, IntegrityError)), transaction.atomic():
        Branch.objects.create(code=bad_length, name="Too long")
