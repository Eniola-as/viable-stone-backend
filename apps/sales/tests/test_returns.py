"""Stage 14 — owner-approved rare returns, refunds and the approval workflow."""

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import EmployeeFactory
from apps.catalog.services.pricing import set_active_price
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    Refund,
    ReturnCondition,
    Sale,
    SaleReturn,
    SaleStatus,
)
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    RequestLine,
    approve_return,
    reject_return,
    submit_return_request,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


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


def _request(sale, actor, item, qty):
    return submit_return_request(
        sale=sale,
        requested_by=actor,
        reason="Customer changed their mind",
        lines=[RequestLine(sale_item_id=item.id, quantity=qty)],
        client_return_id=uuid.uuid4(),
    )


class TestSubmitReturnRequest:
    def test_employee_or_owner_may_submit_for_a_completed_sale(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        emp = EmployeeFactory(branch=branch)
        req = _request(sale, emp, item, 2)
        assert req.request_type == ApprovalType.RETURN
        assert req.status == ApprovalStatus.PENDING
        assert req.requested_by_id == emp.id
        assert req.sale_id == sale.id

    def test_cannot_submit_for_a_non_completed_sale(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        sale.status = SaleStatus.DRAFT
        sale.save(update_fields=["status"], force=True)
        with pytest.raises(APIError):
            _request(sale, owner, item, 1)

    def test_request_quantity_cannot_exceed_sold(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, qty=3)
        with pytest.raises(APIError):
            _request(sale, owner, item, 4)

    def test_pending_request_changes_nothing(self, branch, owner):
        sale, variant, item = _completed_sale(branch, owner, qty=5)
        balance_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        _request(sale, owner, item, 2)
        sale.refresh_from_db()
        assert sale.status == SaleStatus.COMPLETED
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == balance_before
        )
        assert not SaleReturn.objects.exists()
        assert not Refund.objects.exists()


class TestRejectReturn:
    def test_owner_rejects_and_nothing_changes(self, branch, owner):
        sale, variant, item = _completed_sale(branch, owner, qty=5)
        req = _request(sale, owner, item, 2)
        before = InventoryBalance.objects.get(branch=branch, variant=variant).quantity
        rejected = reject_return(
            approval=req, owner=owner, reviewer_note="Out of policy"
        )
        assert rejected.status == ApprovalStatus.REJECTED
        assert rejected.reviewed_by_id == owner.id
        assert rejected.reviewed_at is not None
        sale.refresh_from_db()
        assert sale.status == SaleStatus.COMPLETED
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == before
        )

    def test_rejected_request_is_immutable(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner)
        req = _request(sale, owner, item, 1)
        reject_return(approval=req, owner=owner, reviewer_note="no")
        with pytest.raises(Conflict):
            reject_return(approval=req, owner=owner, reviewer_note="again")
        with pytest.raises(Conflict):
            approve_return(
                approval=req,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id,
                        quantity=1,
                        condition=ReturnCondition.RESELLABLE,
                    )
                ],
                refunds=[RefundLine(method="CASH", amount=Decimal("1000.00"))],
            )


class TestApproveReturn:
    def test_resellable_restores_stock_and_reverses_cogs(self, branch, owner):
        sale, variant, item = _completed_sale(
            branch, owner, price="1000.00", cost="600.00", qty=5
        )
        stock_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        req = _request(sale, owner, item, 2)
        ret = approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
        )
        assert ret.status == "COMPLETED"
        assert ret.total == Decimal("2000.00")  # 2 * original price snapshot
        assert ret.approved_by_id == owner.id

        rline = ret.items.get()
        assert rline.unit_price_snapshot == Decimal("1000.00")
        assert rline.unit_cost_snapshot == Decimal("600.00")
        assert rline.condition == ReturnCondition.RESELLABLE

        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == stock_before + 2
        )
        movement = StockMovement.objects.get(
            variant=variant, movement_type=MovementType.RETURN
        )
        assert movement.quantity_delta == 2
        assert movement.reference_id == ret.id

        req.refresh_from_db()
        assert req.status == ApprovalStatus.APPROVED
        sale.refresh_from_db()
        assert sale.status == SaleStatus.PARTIALLY_RETURNED

    def test_damaged_does_not_restore_stock_or_reverse_cogs(self, branch, owner):
        sale, variant, item = _completed_sale(
            branch, owner, price="1000.00", cost="600.00", qty=5
        )
        stock_before = InventoryBalance.objects.get(
            branch=branch, variant=variant
        ).quantity
        req = _request(sale, owner, item, 2)
        ret = approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.DAMAGED_OR_OPENED,
                )
            ],
            refunds=[
                RefundLine(method="TRANSFER", amount=Decimal("2000.00"), reference="R1")
            ],
        )
        assert ret.total == Decimal("2000.00")
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity
            == stock_before
        )
        assert not StockMovement.objects.filter(
            variant=variant, movement_type=MovementType.RETURN
        ).exists()

    def test_full_return_marks_sale_returned(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="500.00", qty=4)
        req = _request(sale, owner, item, 4)
        approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=4,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
        )
        sale.refresh_from_db()
        assert sale.status == SaleStatus.RETURNED

    def test_partial_then_second_return_respects_remaining_quantity(
        self, branch, owner
    ):
        sale, _v, item = _completed_sale(branch, owner, price="500.00", qty=5)
        r1 = _request(sale, owner, item, 3)
        approve_return(
            approval=r1,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=3,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("1500.00"))],
        )
        r2 = _request(sale, owner, item, 3)  # only 2 remain
        with pytest.raises(APIError):
            approve_return(
                approval=r2,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id,
                        quantity=3,
                        condition=ReturnCondition.RESELLABLE,
                    )
                ],
                refunds=[RefundLine(method="CASH", amount=Decimal("1500.00"))],
            )
        # the allowed remainder works
        r3 = _request(sale, owner, item, 2)
        ret = approve_return(
            approval=r3,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("1000.00"))],
        )
        assert ret.total == Decimal("1000.00")
        sale.refresh_from_db()
        assert sale.status == SaleStatus.RETURNED

    def test_refund_total_must_equal_return_total(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        req = _request(sale, owner, item, 2)
        with pytest.raises(APIError) as exc:
            approve_return(
                approval=req,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id,
                        quantity=2,
                        condition=ReturnCondition.RESELLABLE,
                    )
                ],
                refunds=[RefundLine(method="CASH", amount=Decimal("1999.99"))],
            )
        assert exc.value.code == "refund_mismatch"
        assert not SaleReturn.objects.exists()
        assert not Refund.objects.exists()

    def test_split_refund_allowed(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        req = _request(sale, owner, item, 3)
        ret = approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=3,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[
                RefundLine(method="CASH", amount=Decimal("2000.00")),
                RefundLine(method="POS", amount=Decimal("1000.00"), reference="POS-r"),
            ],
        )
        assert ret.refunds.count() == 2
        assert sum(r.amount for r in ret.refunds.all()) == ret.total
        assert {r.method for r in ret.refunds.all()} == {"CASH", "POS"}
        assert all(r.issued_by_id == owner.id for r in ret.refunds.all())

    def test_refund_uses_original_price_snapshot_not_current_price(self, branch, owner):
        sale, variant, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        # price changes AFTER the sale
        set_active_price(variant=variant, amount=Decimal("5000.00"), changed_by=owner)
        req = _request(sale, owner, item, 2)
        ret = approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
        )
        assert ret.total == Decimal("2000.00")  # 2 * 1000, not 2 * 5000

    def test_approved_return_is_immutable(self, branch, owner):
        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        req = _request(sale, owner, item, 1)
        approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=1,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("1000.00"))],
        )
        with pytest.raises(Conflict):
            reject_return(approval=req, owner=owner, reviewer_note="x")

    def test_idempotent_approval_by_client_return_id(self, branch, owner):
        sale, variant, item = _completed_sale(branch, owner, price="1000.00", qty=5)
        key = uuid.uuid4()
        req1 = submit_return_request(
            sale=sale,
            requested_by=owner,
            reason="dup guard",
            lines=[RequestLine(sale_item_id=item.id, quantity=2)],
            client_return_id=key,
        )
        first = approve_return(
            approval=req1,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
            client_return_id=key,
        )
        # a second approval attempt with the same key returns the same return,
        # and does not double the stock / refund
        req2 = submit_return_request(
            sale=sale,
            requested_by=owner,
            reason="retry",
            lines=[RequestLine(sale_item_id=item.id, quantity=2)],
            client_return_id=key,
        )
        second = approve_return(
            approval=req2,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=2,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
            client_return_id=key,
        )
        assert first.id == second.id
        assert SaleReturn.objects.count() == 1
        assert Refund.objects.count() == 1
        assert (
            StockMovement.objects.filter(
                variant=variant, movement_type=MovementType.RETURN
            ).count()
            == 1
        )


class TestModelConstraints:
    def test_salereturn_unique_branch_client_return_id(self, branch, owner):
        sale, _v, _item = _completed_sale(branch, owner)
        approval = ApprovalRequest.objects.create(
            branch=branch,
            sale=sale,
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.APPROVED,
            requested_by=owner,
            reason="x",
        )
        approval2 = ApprovalRequest.objects.create(
            branch=branch,
            sale=sale,
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.APPROVED,
            requested_by=owner,
            reason="y",
        )
        key = uuid.uuid4()
        SaleReturn.objects.create(
            branch=branch,
            original_sale=sale,
            approval=approval,
            total=Decimal("10.00"),
            approved_by=owner,
            client_return_id=key,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SaleReturn.objects.create(
                branch=branch,
                original_sale=sale,
                approval=approval2,
                total=Decimal("10.00"),
                approved_by=owner,
                client_return_id=key,
            )

    def test_refund_amount_must_be_positive(self, branch, owner):
        sale, _v, _item = _completed_sale(branch, owner)
        approval = ApprovalRequest.objects.create(
            branch=branch,
            sale=sale,
            request_type=ApprovalType.RETURN,
            status=ApprovalStatus.APPROVED,
            requested_by=owner,
            reason="x",
        )
        ret = SaleReturn.objects.create(
            branch=branch,
            original_sale=sale,
            approval=approval,
            total=Decimal("10.00"),
            approved_by=owner,
            client_return_id=uuid.uuid4(),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Refund.objects.create(
                sale_return=ret, method="CASH", amount=Decimal("0.00"), issued_by=owner
            )


class TestReceiptUnchanged:
    def test_original_receipt_is_untouched_after_a_return(self, branch, owner):
        from apps.sales.services.receipts import receipt_context

        sale, _v, item = _completed_sale(branch, owner, price="1000.00", qty=3)
        before = receipt_context(sale)
        req = _request(sale, owner, item, 1)
        approve_return(
            approval=req,
            owner=owner,
            lines=[
                ApprovedLine(
                    sale_item_id=item.id,
                    quantity=1,
                    condition=ReturnCondition.RESELLABLE,
                )
            ],
            refunds=[RefundLine(method="CASH", amount=Decimal("1000.00"))],
        )
        after = receipt_context(Sale.objects.get(pk=sale.pk))
        assert before["items"] == after["items"]
        assert before["total"] == after["total"]
        assert before["receipt_number"] == after["receipt_number"]
