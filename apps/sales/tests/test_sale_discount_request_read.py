"""G22 — SaleRead exposes a minimal, nullable discount-approval summary.

Option A: a read-only ``discount_request`` object on ``SaleRead``, populated
whenever a DISCOUNT ``ApprovalRequest`` exists for the sale (any status),
``null`` otherwise. Visible to exactly the users who can already retrieve the
sale (owner: branch-scoped; cashier: own sales). No unrelated approvals, no
cross-branch data, no owner-only financials, no reviewer identity beyond the
username, no fingerprint.
"""

import json
import uuid

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    Sale,
)
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales"
APPROVALS = "/api/v1/approvals"

_SUMMARY_KEYS = {
    "id",
    "status",
    "requested_amount",
    "approved_amount",
    "reason",
    "reviewer_note",
    "requested_by_username",
    "reviewed_by_username",
    "reviewed_at",
    "created_at",
}


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _draft(client, branch, owner, *, q1=3, q2=4):
    v1 = stocked_variant(branch, owner, price="1000.00", quantity=20, unit_cost="600")
    v2 = stocked_variant(branch, owner, price="500.00", quantity=20, unit_cost="250")
    res = client.post(
        f"{SALES}/drafts/",
        {
            "client_sale_id": str(uuid.uuid4()),
            "items": [
                {"variant": str(v1.id), "quantity": q1},
                {"variant": str(v2.id), "quantity": q2},
            ],
        },
        format="json",
    )
    return res.json()["id"], v1, v2


def _request_discount(client, sale_id, *, amount="1000.00", reason="bulk buyer"):
    return client.post(
        f"{SALES}/{sale_id}/discount-requests/",
        {"amount": amount, "reason": reason},
        format="json",
    ).json()


def _get(client, sale_id):
    return client.get(f"{SALES}/{sale_id}/")


# --------------------------------------------------------------------------- #
# 1. no request -> null                                                       #
# --------------------------------------------------------------------------- #


class TestNoRequest:
    def test_draft_with_no_discount_request_returns_null(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        body = _get(cc, sale_id).json()
        assert "discount_request" in body
        assert body["discount_request"] is None

    def test_completed_plain_sale_returns_null(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        cc = login_as(cashier)
        sale_id = cc.post(
            f"{SALES}/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 1}],
                "payments": [{"method": "CASH", "amount": "1000.00"}],
            },
            format="json",
        ).json()["id"]
        assert _get(cc, sale_id).json()["discount_request"] is None

    def test_unrelated_return_approval_does_not_populate_discount_request(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        ApprovalRequest.objects.create(
            branch=branch,
            sale=Sale.objects.get(pk=sale_id),
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.PENDING,
            requested_by=cashier,
            reason="not a discount",
        )
        assert _get(cc, sale_id).json()["discount_request"] is None


# --------------------------------------------------------------------------- #
# 2. lifecycle: pending / approved / rejected / superseded                     #
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_pending(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        appr = _request_discount(cc, sale_id, amount="1000.00", reason="bulk buyer")

        body = _get(cc, sale_id).json()
        dr = body["discount_request"]
        assert set(dr) == _SUMMARY_KEYS
        assert dr["id"] == appr["id"]
        assert dr["status"] == "PENDING"
        assert dr["requested_amount"] == "1000.00"
        assert dr["approved_amount"] is None
        assert dr["reason"] == "bulk buyer"
        assert dr["reviewer_note"] == ""
        assert dr["requested_by_username"] == cashier.username
        assert dr["reviewed_by_username"] is None
        assert dr["reviewed_at"] is None
        assert dr["created_at"]
        # sale itself: PENDING_APPROVAL, discount_total still zero
        assert body["status"] == "PENDING_APPROVAL"
        assert body["discount_total"] == "0.00"

    def test_approved_sets_amount_and_reviewer_but_not_sale_status(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        rid = _request_discount(cc, sale_id, amount="1000.00")["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {"amount": "1000.00", "reviewer_note": "ok bulk"},
            format="json",
        )
        body = _get(cc, sale_id).json()
        dr = body["discount_request"]
        assert dr["status"] == "APPROVED"
        assert dr["approved_amount"] == "1000.00"
        assert dr["requested_amount"] == "1000.00"
        assert dr["reviewer_note"] == "ok bulk"
        assert dr["reviewed_by_username"] == owner.username
        assert dr["reviewed_at"] is not None
        # G22 lifecycle fact: approve does NOT move Sale.status; it stays
        # PENDING_APPROVAL until finalise. discount_total == approved amount.
        assert body["status"] == "PENDING_APPROVAL"
        assert body["discount_total"] == "1000.00"

    def test_rejected_sets_status_and_sale_reverts_to_draft(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        rid = _request_discount(cc, sale_id, amount="1000.00")["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/reject/", {"reviewer_note": "too much"}, format="json"
        )
        body = _get(cc, sale_id).json()
        dr = body["discount_request"]
        assert dr["status"] == "REJECTED"
        assert dr["approved_amount"] is None
        assert dr["reviewer_note"] == "too much"
        assert dr["reviewed_by_username"] == owner.username
        # G22 lifecycle fact: reject reverts Sale.status DRAFT, discount_total 0.
        assert body["status"] == "DRAFT"
        assert body["discount_total"] == "0.00"

    def test_completed_discounted_sale_keeps_approved_summary(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        rid = _request_discount(cc, sale_id, amount="1000.00")["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        cc.post(
            f"{SALES}/{sale_id}/finalise/",
            {
                "payments": [
                    {
                        "method": "CASH",
                        "amount": "4000.00",
                        "tendered_amount": "4000.00",
                    }
                ]
            },
            format="json",
        )
        body = _get(cc, sale_id).json()
        assert body["status"] == "COMPLETED"
        assert body["discount_request"]["status"] == "APPROVED"
        assert body["discount_request"]["approved_amount"] == "1000.00"
        assert body["discount_total"] == "1000.00"

    def test_cart_edit_supersedes_pending_request_row_is_kept_as_rejected(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, v1, _v2 = _draft(cc, branch, owner)
        _request_discount(cc, sale_id, amount="1000.00")
        cc.put(
            f"{SALES}/{sale_id}/draft-cart/",
            {"items": [{"variant": str(v1.id), "quantity": 1}]},
            format="json",
        )
        body = _get(cc, sale_id).json()
        dr = body["discount_request"]
        # No CANCELLED/EXPIRED enum in the model: a superseded request is a
        # REJECTED row with no reviewer and a "Superseded: ..." reviewer_note.
        assert dr is not None
        assert dr["status"] == "REJECTED"
        assert dr["reviewed_by_username"] is None
        assert dr["reviewer_note"].startswith("Superseded")
        assert body["status"] == "DRAFT"

    def test_cancel_draft_supersedes_pending_request(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        _request_discount(cc, sale_id, amount="1000.00")
        cc.post(f"{SALES}/{sale_id}/cancel/")
        body = _get(cc, sale_id).json()
        assert body["status"] == "CANCELLED"
        dr = body["discount_request"]
        assert dr["status"] == "REJECTED"
        assert dr["reviewed_by_username"] is None

    def test_summary_reflects_the_most_recent_discount_request(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, v1, _v2 = _draft(cc, branch, owner)
        _request_discount(cc, sale_id, amount="1000.00", reason="first")
        cc.put(
            f"{SALES}/{sale_id}/draft-cart/",
            {"items": [{"variant": str(v1.id), "quantity": 5}]},
            format="json",
        )
        second = _request_discount(cc, sale_id, amount="900.00", reason="second try")
        dr = _get(cc, sale_id).json()["discount_request"]
        assert dr["id"] == second["id"]
        assert dr["status"] == "PENDING"
        assert dr["reason"] == "second try"
        assert dr["requested_amount"] == "900.00"


# --------------------------------------------------------------------------- #
# 4/6. visibility: owner, sale's cashier, unrelated cashier, cross-branch      #
# --------------------------------------------------------------------------- #


class TestVisibility:
    def _pending_sale(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        _request_discount(cc, sale_id, amount="1000.00")
        return cashier, sale_id

    def test_branch_owner_sees_the_summary(self, login_as, branch, owner):
        _cashier, sale_id = self._pending_sale(login_as, branch, owner)
        dr = _get(login_as(owner), sale_id).json()["discount_request"]
        assert dr["status"] == "PENDING"

    def test_sales_own_cashier_sees_the_summary(self, login_as, branch, owner):
        cashier, sale_id = self._pending_sale(login_as, branch, owner)
        dr = _get(login_as(cashier), sale_id).json()["discount_request"]
        assert dr["status"] == "PENDING"

    def test_unrelated_cashier_gets_404_never_the_summary(
        self, login_as, branch, owner
    ):
        _cashier, sale_id = self._pending_sale(login_as, branch, owner)
        other = EmployeeFactory(branch=branch)
        assert _get(login_as(other), sale_id).status_code == 404

    def test_other_branch_owner_gets_404(self, login_as, branch, owner):
        _cashier, sale_id = self._pending_sale(login_as, branch, owner)
        other = BranchFactory(code="VS73")
        assert _get(login_as(OwnerFactory(branch=other)), sale_id).status_code == 404


# --------------------------------------------------------------------------- #
# 7. list-query performance: no N+1                                            #
# --------------------------------------------------------------------------- #


class TestListPerformance:
    def _make_pending_draft(self, cc, branch, owner):
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        _request_discount(cc, sale_id, amount="1000.00")
        return sale_id

    def test_sale_list_discount_request_query_count_is_constant(
        self, login_as, branch, owner, django_assert_max_num_queries
    ):
        oc = login_as(owner)
        self._make_pending_draft(oc, branch, owner)
        with django_assert_max_num_queries(50) as ctx:
            r1 = oc.get(f"{SALES}/")
        assert r1.status_code == 200
        assert any(row["discount_request"] for row in r1.json()["results"])
        baseline = len(ctx.captured_queries)

        for _ in range(3):
            self._make_pending_draft(oc, branch, owner)
        with django_assert_max_num_queries(baseline) as ctx2:
            r2 = oc.get(f"{SALES}/")
        assert len({row["id"] for row in r2.json()["results"]}) >= 4
        assert len(ctx2.captured_queries) == baseline


# --------------------------------------------------------------------------- #
# 5. no sensitive-field leakage                                                #
# --------------------------------------------------------------------------- #


class TestNoLeakage:
    def test_summary_carries_no_fingerprint_cost_or_reviewer_id(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        cc = login_as(cashier)
        sale_id, _v1, _v2 = _draft(cc, branch, owner)
        rid = _request_discount(cc, sale_id, amount="1000.00")["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        dr = _get(cc, sale_id).json()["discount_request"]
        blob = json.dumps(dr)
        assert set(dr) == _SUMMARY_KEYS
        # only the *_username projections are exposed, never the raw uuid keys
        assert '"reviewed_by":' not in blob
        assert '"requested_by":' not in blob
        for forbidden in (
            "fingerprint",
            "requested_changes",
            "subtotal",
            "unit_cost",
            "average_unit_cost",
            "cogs",
            "profit",
            "mfa",
        ):
            assert forbidden not in blob


# --------------------------------------------------------------------------- #
# 10. OpenAPI contract                                                         #
# --------------------------------------------------------------------------- #


class TestOpenAPIContract:
    def test_saleread_documents_nullable_discount_request(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        sale_read = schema["components"]["schemas"]["SaleRead"]["properties"]
        assert "discount_request" in sale_read
        summary = schema["components"]["schemas"]["SaleDiscountRequestSummary"][
            "properties"
        ]
        assert set(summary) == _SUMMARY_KEYS
        # reuses the canonical ApprovalStatus enum, not a new one
        assert "$ref" in summary["status"] or "allOf" in summary["status"]
