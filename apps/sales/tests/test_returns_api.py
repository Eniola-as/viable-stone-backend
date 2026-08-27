"""Stage 14 — return workflow API: submit, list, approve, reject, permissions."""

import uuid
from decimal import Decimal

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import Refund, SaleReturn, SaleStatus
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales"
APPROVALS = "/api/v1/approvals"
RETURNS = "/api/v1/returns/"


def _completed_sale(branch, owner, *, price="1000.00", cost="600.00", qty=5):
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(
        branch, owner, price=price, quantity=qty + 10, unit_cost=cost
    )
    sale = create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=qty)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal(price) * qty,
                tendered_amount=Decimal(price) * qty,
            )
        ],
        client_sale_id=uuid.uuid4(),
    )
    return sale, variant, sale.items.get()


def _submit(client, sale, item, qty, key=None):
    return client.post(
        f"{SALES}/{sale.id}/return-requests/",
        {
            "reason": "Wrong colour supplied to the customer",
            "client_return_id": str(key or uuid.uuid4()),
            "lines": [{"sale_item": str(item.id), "quantity": qty}],
        },
        format="json",
    )


class TestSubmitReturnRequestApi:
    def test_employee_can_submit(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        emp = EmployeeFactory(branch=branch)
        res = _submit(login_as(emp), sale, item, 2)
        assert res.status_code == 201, res.content
        body = res.json()
        assert body["request_type"] == "RETURN"
        assert body["status"] == "PENDING"

    def test_owner_can_submit(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        assert _submit(login_as(owner), sale, item, 1).status_code == 201

    def test_cross_branch_sale_is_404(self, login_as, owner):
        other = BranchFactory(code="VS63")
        other_owner = OwnerFactory(branch=other)
        sale, _v, item = _completed_sale(other, other_owner)
        assert _submit(login_as(owner), sale, item, 1).status_code == 404


class TestApprovalListing:
    def test_owner_sees_branch_requests_employee_sees_own(
        self, login_as, branch, owner
    ):
        sale, _v, item = _completed_sale(branch, owner)
        mine = EmployeeFactory(branch=branch, username="mine")
        other = EmployeeFactory(branch=branch, username="other")
        _submit(login_as(mine), sale, item, 1)
        _submit(login_as(other), sale, item, 1)

        owner_body = login_as(owner).get(f"{APPROVALS}/").json()
        assert owner_body["count"] == 2
        mine_body = login_as(mine).get(f"{APPROVALS}/").json()
        assert mine_body["count"] == 1


class TestApproveRejectApi:
    def test_employee_cannot_approve_or_reject(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        emp = EmployeeFactory(branch=branch)
        rid = _submit(login_as(emp), sale, item, 2).json()["id"]
        assert (
            login_as(emp)
            .post(f"{APPROVALS}/{rid}/approve/", {}, format="json")
            .status_code
            == 403
        )
        assert (
            login_as(emp)
            .post(f"{APPROVALS}/{rid}/reject/", {}, format="json")
            .status_code
            == 403
        )

    def test_owner_approves_resellable_return(self, login_as, branch, owner):
        sale, variant, item = _completed_sale(
            branch, owner, price="1000.00", cost="600.00", qty=5
        )
        stock_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        rid = _submit(login_as(owner), sale, item, 2).json()["id"]
        res = login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {
                "reviewer_note": "Approved as a goodwill exception",
                "lines": [
                    {
                        "sale_item": str(item.id),
                        "quantity": 2,
                        "condition": "RESELLABLE",
                    }
                ],
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
            format="json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["total"] == "2000.00"
        assert body["status"] == "COMPLETED"
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == stock_before + 2
        )
        assert (
            StockMovement.objects.filter(
                variant=variant, movement_type=MovementType.RETURN
            ).count()
            == 1
        )
        sale.refresh_from_db()
        assert sale.status == SaleStatus.PARTIALLY_RETURNED

    def test_split_refund_via_api(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        rid = _submit(login_as(owner), sale, item, 3).json()["id"]
        res = login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {
                "lines": [
                    {
                        "sale_item": str(item.id),
                        "quantity": 3,
                        "condition": "RESELLABLE",
                    }
                ],
                "refunds": [
                    {"method": "CASH", "amount": "2000.00"},
                    {"method": "POS", "amount": "1000.00", "reference": "POS-R"},
                ],
            },
            format="json",
        )
        assert res.status_code == 200
        assert len(res.json()["refunds"]) == 2

    def test_refund_mismatch_is_400_and_no_records(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        rid = _submit(login_as(owner), sale, item, 2).json()["id"]
        res = login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {
                "lines": [
                    {
                        "sale_item": str(item.id),
                        "quantity": 2,
                        "condition": "RESELLABLE",
                    }
                ],
                "refunds": [{"method": "CASH", "amount": "1999.99"}],
            },
            format="json",
        )
        assert res.status_code == 400
        assert res.json()["code"] == "refund_mismatch"
        assert not SaleReturn.objects.exists()
        assert not Refund.objects.exists()

    def test_double_decision_is_409(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="500.00", qty=2)
        rid = _submit(login_as(owner), sale, item, 1).json()["id"]
        payload = {
            "lines": [
                {"sale_item": str(item.id), "quantity": 1, "condition": "RESELLABLE"}
            ],
            "refunds": [{"method": "CASH", "amount": "500.00"}],
        }
        assert (
            login_as(owner)
            .post(f"{APPROVALS}/{rid}/approve/", payload, format="json")
            .status_code
            == 200
        )
        assert (
            login_as(owner)
            .post(f"{APPROVALS}/{rid}/reject/", {}, format="json")
            .status_code
            == 409
        )

    def test_reject_changes_nothing(self, login_as, branch, owner):
        sale, variant, item = _completed_sale(branch, owner, qty=5)
        before = InventoryBalance.objects.get(branch=branch, variant=variant).quantity
        rid = _submit(login_as(owner), sale, item, 2).json()["id"]
        res = login_as(owner).post(
            f"{APPROVALS}/{rid}/reject/", {"reviewer_note": "no"}, format="json"
        )
        assert res.status_code == 200
        assert res.json()["status"] == "REJECTED"
        sale.refresh_from_db()
        assert sale.status == SaleStatus.COMPLETED
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == before
        )
        assert not SaleReturn.objects.exists()

    def test_cross_branch_approval_is_404(self, login_as, owner):
        other = BranchFactory(code="VS64")
        other_owner = OwnerFactory(branch=other)
        sale, _v, item = _completed_sale(other, other_owner)
        from apps.sales.services.returns import RequestLine, submit_return_request

        req = submit_return_request(
            sale=sale,
            requested_by=other_owner,
            reason="x",
            lines=[RequestLine(sale_item_id=item.id, quantity=1)],
            client_return_id=uuid.uuid4(),
        )
        assert (
            login_as(owner)
            .post(f"{APPROVALS}/{req.id}/reject/", {}, format="json")
            .status_code
            == 404
        )


class TestReturnsReadApi:
    def test_returns_listed_and_retrievable(self, login_as, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        rid = _submit(login_as(owner), sale, item, 2).json()["id"]
        login_as(owner).post(
            f"{APPROVALS}/{rid}/approve/",
            {
                "lines": [
                    {
                        "sale_item": str(item.id),
                        "quantity": 2,
                        "condition": "RESELLABLE",
                    }
                ],
                "refunds": [{"method": "CASH", "amount": "2000.00"}],
            },
            format="json",
        )
        body = login_as(owner).get(RETURNS).json()
        assert body["count"] == 1
        return_id = body["results"][0]["id"]
        detail = login_as(owner).get(f"{RETURNS}{return_id}/").json()
        assert detail["total"] == "2000.00"
        assert len(detail["items"]) == 1
        assert detail["items"][0]["unit_price_snapshot"] == "1000.00"
