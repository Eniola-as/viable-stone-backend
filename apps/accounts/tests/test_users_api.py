"""Role/branch permission tests for user and branch management."""

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import BranchFactory, EmployeeFactory

pytestmark = pytest.mark.django_db

USERS = "/api/v1/users/"
BRANCHES = "/api/v1/branches/"


class TestUserManagementPermissions:
    def test_employee_cannot_list_users(self, login_as, employee):
        assert login_as(employee).get(USERS).status_code == 403

    def test_owner_can_create_employee_in_own_branch(self, login_as, owner, branch):
        client = login_as(owner)
        res = client.post(
            USERS,
            {
                "username": "newhire",
                "password": "new-hire-pass-123",
                "role": "EMPLOYEE",
            },
        )
        assert res.status_code == 201, res.content
        created = User.objects.get(username="newhire")
        assert created.branch_id == branch.id
        assert created.must_change_password is True
        assert "password" not in res.json()

    def test_owner_cannot_create_tech_admin(self, login_as, owner):
        res = login_as(owner).post(
            USERS,
            {"username": "sneaky", "password": "x-pass-123456", "role": "TECH_ADMIN"},
        )
        assert res.status_code == 400
        assert "role" in res.json()["field_errors"]

    def test_client_cannot_assign_a_different_branch(self, login_as, owner, branch):
        other = BranchFactory(code="VS99")
        client = login_as(owner)
        res = client.post(
            USERS,
            {
                "username": "hire2",
                "password": "hire2-pass-123",
                "role": "EMPLOYEE",
                "branch": str(other.id),
            },
        )
        assert res.status_code == 201
        assert User.objects.get(username="hire2").branch_id == branch.id

    def test_owner_only_sees_own_branch_users(self, login_as, owner):
        EmployeeFactory(branch=owner.branch, username="mine")
        theirs = EmployeeFactory(branch=BranchFactory(code="VS42"), username="theirs")
        body = login_as(owner).get(USERS).json()
        usernames = {row["username"] for row in body["results"]}
        assert "mine" in usernames
        assert "theirs" not in usernames
        assert str(theirs.id) not in str(body)

    def test_cross_branch_user_id_is_404_not_403(self, login_as, owner):
        outsider = EmployeeFactory(branch=BranchFactory(code="VS43"))
        res = login_as(owner).get(f"{USERS}{outsider.id}/")
        assert res.status_code == 404

    def test_delete_is_blocked_use_deactivate(self, login_as, owner):
        target = EmployeeFactory(branch=owner.branch)
        res = login_as(owner).delete(f"{USERS}{target.id}/")
        assert res.status_code == 405

    def test_deactivate_action(self, login_as, owner):
        target = EmployeeFactory(branch=owner.branch, username="leaver")
        res = login_as(owner).post(f"{USERS}{target.id}/deactivate/")
        assert res.status_code == 200
        target.refresh_from_db()
        assert target.is_active is False


class TestBranchPermissions:
    def test_employee_can_read_branches(self, login_as, employee):
        assert login_as(employee).get(BRANCHES).status_code == 200

    def test_employee_cannot_create_branch(self, login_as, employee):
        res = login_as(employee).post(BRANCHES, {"code": "VS02", "name": "New"})
        assert res.status_code == 403

    def test_owner_sees_only_their_branch(self, login_as, owner):
        BranchFactory(code="VS77")
        body = login_as(owner).get(BRANCHES).json()
        assert {row["code"] for row in body["results"]} == {"VS01"}
