"""Stage 14B — discount workflow API: draft, request, approve, finalise, receipt."""

import uuid

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.models import Payment, Sale, SaleStatus
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales"
APPROVALS = "/api/v1/approvals"


def _draft(client, branch, owner, *, q1=3, q2=4):
    v1 = stocked_variant(
        branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
    )
    v2 = stocked_variant(branch, owner, price="500.00", quantity=20, unit_cost="250.00")
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
    return res, v1, v2


class TestDraftAndRequest:
    def test_cashier_creates_draft_no_side_effects(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        res, _v1, _v2 = _draft(login_as(cashier), branch, owner)
        assert res.status_code == 201, res.content
        body = res.json()
        assert body["status"] == "DRAFT"
        assert body["subtotal"] == "5000.00"
        assert body["total"] == "5000.00"
        assert body["receipt_number"] is None
        assert not Payment.objects.exists()

    def test_request_discount_requires_reason(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale_id = _draft(login_as(cashier), branch, owner)[0].json()["id"]
        res = login_as(cashier).post(
            f"{SALES}/{sale_id}/discount-requests/",
            {"amount": "500.00", "reason": ""},
            format="json",
        )
        assert res.status_code == 400

    def test_request_discount_moves_to_pending(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale_id = _draft(login_as(cashier), branch, owner)[0].json()["id"]
        res = login_as(cashier).post(
            f"{SALES}/{sale_id}/discount-requests/",
            {"amount": "1000.00", "reason": "bulk buyer"},
            format="json",
        )
        assert res.status_code == 201
        assert res.json()["request_type"] == "DISCOUNT"
        assert Sale.objects.get(pk=sale_id).status == SaleStatus.PENDING_APPROVAL


class TestApproveAndFinalise:
    def _pending(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        c = login_as(cashier)
        sale_id = _draft(c, branch, owner)[0].json()["id"]
        rid = c.post(
            f"{SALES}/{sale_id}/discount-requests/",
            {"amount": "1000.00", "reason": "x"},
            format="json",
        ).json()["id"]
        return cashier, sale_id, rid

    def test_employee_cannot_approve_discount(self, login_as, branch, owner):
        cashier, _sid, rid = self._pending(login_as, branch, owner)
        res = login_as(cashier).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        assert res.status_code == 403

    def test_owner_approves_then_cashier_finalises_with_discount(
        self, login_as, branch, owner
    ):
        cashier, sale_id, rid = self._pending(login_as, branch, owner)
        appr = login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {"amount": "1000.00", "reviewer_note": "ok"},
            format="json",
        )
        assert appr.status_code == 200
        assert appr.json()["status"] == "APPROVED"

        fin = login_as(cashier).post(
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
        assert fin.status_code == 200, fin.content
        body = fin.json()
        assert body["status"] == "COMPLETED"
        assert body["subtotal"] == "5000.00"
        assert body["discount_total"] == "1000.00"
        assert body["total"] == "4000.00"
        assert body["receipt_number"]

    def test_finalise_payment_must_equal_discounted_total(
        self, login_as, branch, owner
    ):
        cashier, sale_id, rid = self._pending(login_as, branch, owner)
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        res = login_as(cashier).post(
            f"{SALES}/{sale_id}/finalise/",
            {
                "payments": [
                    {
                        "method": "CASH",
                        "amount": "5000.00",
                        "tendered_amount": "5000.00",
                    }
                ]
            },
            format="json",
        )
        assert res.status_code == 400
        assert res.json()["code"] == "payment_mismatch"

    def test_finalise_split_payment(self, login_as, branch, owner):
        cashier, sale_id, rid = self._pending(login_as, branch, owner)
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        res = login_as(cashier).post(
            f"{SALES}/{sale_id}/finalise/",
            {
                "payments": [
                    {"method": "POS", "amount": "2500.00", "reference": "P"},
                    {
                        "method": "CASH",
                        "amount": "1500.00",
                        "tendered_amount": "2000.00",
                    },
                ]
            },
            format="json",
        )
        assert res.status_code == 200
        assert len(res.json()["payments"]) == 2
        assert res.json()["change_due"] == "500.00"

    def test_reject_reverts_to_draft(self, login_as, branch, owner):
        _c, sale_id, rid = self._pending(login_as, branch, owner)
        res = login_as(owner).post(
            f"{APPROVALS}/{rid}/reject/", {"reviewer_note": "no"}, format="json"
        )
        assert res.status_code == 200
        assert res.json()["status"] == "REJECTED"
        assert Sale.objects.get(pk=sale_id).status == SaleStatus.DRAFT

    def test_cancel_draft(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        c = login_as(cashier)
        sale_id = _draft(c, branch, owner)[0].json()["id"]
        assert c.post(f"{SALES}/{sale_id}/cancel/").status_code == 200
        assert Sale.objects.get(pk=sale_id).status == SaleStatus.CANCELLED

    def test_cart_edit_invalidates_pending_and_needs_new_request(
        self, login_as, branch, owner
    ):
        cashier, sale_id, rid = self._pending(login_as, branch, owner)
        _r, v1, _v2 = _draft(login_as(cashier), branch, owner)  # unused vars ok
        edit = login_as(cashier).put(
            f"{SALES}/{sale_id}/draft-cart/",
            {"items": [{"variant": str(v1.id), "quantity": 1}]},
            format="json",
        )
        assert edit.status_code == 200
        assert edit.json()["status"] == "DRAFT"
        # the old approval request is now REJECTED
        got = login_as(owner).get(f"{APPROVALS}/{rid}/").json()
        assert got["status"] == "REJECTED"

    def test_cross_branch_draft_is_404(self, login_as, owner):
        other = BranchFactory(code="VS65")
        other_owner = OwnerFactory(branch=other)
        other_cashier = EmployeeFactory(branch=other)
        sale_id = _draft(login_as(other_cashier), other, other_owner)[0].json()["id"]
        assert login_as(owner).post(f"{SALES}/{sale_id}/cancel/").status_code == 404


class TestDiscountedReceipt:
    def _completed_discounted(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        c = login_as(cashier)
        sale_id = _draft(c, branch, owner)[0].json()["id"]
        rid = c.post(
            f"{SALES}/{sale_id}/discount-requests/",
            {"amount": "1000.00", "reason": "x"},
            format="json",
        ).json()["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/", {"amount": "1000.00"}, format="json"
        )
        c.post(
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
        return c, sale_id

    def test_json_receipt_shows_subtotal_discount_total(self, login_as, branch, owner):
        c, sale_id = self._completed_discounted(login_as, branch, owner)
        body = c.get(f"{SALES}/{sale_id}/receipt/").json()
        assert body["subtotal"] == "5,000.00"
        assert body["discount_total"] == "1,000.00"
        assert body["total"] == "4,000.00"

    def test_pdf_receipt_shows_discount_line(self, login_as, branch, owner):
        import io

        from pypdf import PdfReader

        c, sale_id = self._completed_discounted(login_as, branch, owner)
        res = c.get(f"{SALES}/{sale_id}/receipt.pdf")
        assert res.status_code == 200
        text = "\n".join(
            (p.extract_text() or "") for p in PdfReader(io.BytesIO(res.content)).pages
        )
        assert "Subtotal" in text
        assert "Discount" in text
        assert "NGN 5,000.00" in text
        assert "NGN 4,000.00" in text
