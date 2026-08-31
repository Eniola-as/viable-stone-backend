"""G21 — GET /api/v1/approvals/ gains validated ?status / ?request_type filters.

Without them a pending request can slip onto page 2+ of a long approval history
and vanish from the owner's "Pending" tab. The filters compose with search,
ordering and pagination, preserve owner/branch/cashier scoping, and reject
invalid values with the standard validation envelope.
"""

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
)

pytestmark = pytest.mark.django_db

APPROVALS = "/api/v1/approvals/"
SALES = "/api/v1/sales"


def _mk(branch, user, *, request_type, status, sale=None):
    return ApprovalRequest.objects.create(
        branch=branch,
        sale=sale,
        request_type=request_type,
        status=status,
        requested_by=user,
        reason="seed",
        reviewed_by=user if status != ApprovalStatus.PENDING else None,
    )


@pytest.fixture
def seeded(branch, owner):
    cashier = EmployeeFactory(branch=branch)
    rows = {
        "pending_discount": _mk(
            branch,
            cashier,
            request_type=ApprovalType.DISCOUNT,
            status=ApprovalStatus.PENDING,
        ),
        "approved_discount": _mk(
            branch,
            cashier,
            request_type=ApprovalType.DISCOUNT,
            status=ApprovalStatus.APPROVED,
        ),
        "pending_return": _mk(
            branch,
            cashier,
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.PENDING,
        ),
        "rejected_return": _mk(
            branch,
            cashier,
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.REJECTED,
        ),
    }
    return cashier, rows


class TestStatusFilter:
    def test_status_pending_returns_only_pending(self, login_as, owner, seeded):
        _c, rows = seeded
        body = login_as(owner).get(f"{APPROVALS}?status=PENDING").json()
        ids = {r["id"] for r in body["results"]}
        assert ids == {str(rows["pending_discount"].id), str(rows["pending_return"].id)}
        assert body["count"] == 2

    def test_status_approved_returns_only_approved(self, login_as, owner, seeded):
        _c, rows = seeded
        body = login_as(owner).get(f"{APPROVALS}?status=APPROVED").json()
        assert {r["id"] for r in body["results"]} == {str(rows["approved_discount"].id)}


class TestRequestTypeFilter:
    def test_request_type_discount(self, login_as, owner, seeded):
        _c, rows = seeded
        body = login_as(owner).get(f"{APPROVALS}?request_type=DISCOUNT").json()
        assert {r["id"] for r in body["results"]} == {
            str(rows["pending_discount"].id),
            str(rows["approved_discount"].id),
        }

    def test_combined_status_and_request_type(self, login_as, owner, seeded):
        _c, rows = seeded
        body = (
            login_as(owner)
            .get(f"{APPROVALS}?status=PENDING&request_type=RETURN")
            .json()
        )
        assert {r["id"] for r in body["results"]} == {str(rows["pending_return"].id)}

    def test_composes_with_pagination_and_ordering(self, login_as, owner, seeded):
        res = login_as(owner).get(
            f"{APPROVALS}?status=PENDING&ordering=created_at&page_size=1"
        )
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 2
        assert len(body["results"]) == 1
        assert body["next"]


class TestInvalidValues:
    def test_invalid_status_is_a_validation_error(self, login_as, owner, seeded):
        res = login_as(owner).get(f"{APPROVALS}?status=NOPE")
        assert res.status_code == 400
        body = res.json()
        assert body["code"] == "validation_error"
        assert "status" in body["field_errors"]

    def test_invalid_request_type_is_a_validation_error(self, login_as, owner, seeded):
        res = login_as(owner).get(f"{APPROVALS}?request_type=NOPE")
        assert res.status_code == 400
        assert res.json()["code"] == "validation_error"


class TestScopingPreserved:
    def test_cashier_only_sees_own_requests_even_with_filter(
        self, login_as, branch, owner, seeded
    ):
        _seed_cashier, _rows = seeded
        other = EmployeeFactory(branch=branch)
        # a pending discount owned by `other`
        mine = _mk(
            branch,
            other,
            request_type=ApprovalType.DISCOUNT,
            status=ApprovalStatus.PENDING,
        )
        body = login_as(other).get(f"{APPROVALS}?status=PENDING").json()
        assert {r["id"] for r in body["results"]} == {str(mine.id)}

    def test_cross_branch_rows_never_appear(self, login_as, owner, seeded):
        other_branch = BranchFactory(code="VS68")
        other_owner = OwnerFactory(branch=other_branch)
        _mk(
            other_branch,
            other_owner,
            request_type=ApprovalType.DISCOUNT,
            status=ApprovalStatus.PENDING,
        )
        body = login_as(owner).get(f"{APPROVALS}?status=PENDING").json()
        assert body["count"] == 2  # only this branch's two pending rows


class TestOpenAPI:
    def test_approvals_list_documents_status_and_request_type(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        params = {
            p["name"]
            for p in schema["paths"]["/api/v1/approvals/"]["get"]["parameters"]
        }
        assert {"status", "request_type"} <= params
