"""Stage 14B — owner-approved fixed-Naira discount on an internal DRAFT sale."""

import uuid
from decimal import Decimal

import pytest

from apps.accounts.tests.factories import EmployeeFactory
from apps.catalog.services.pricing import set_active_price
from apps.core.exceptions import APIError, Conflict
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    ApprovalStatus,
    ApprovalType,
    Payment,
    Sale,
    SaleStatus,
)
from apps.sales.services.discounts import (
    allocate_discount,
    approve_discount,
    cancel_draft,
    create_draft_sale,
    draft_fingerprint,
    finalise_draft,
    reject_discount,
    replace_draft_cart,
    request_discount,
)
from apps.sales.services.sales import CartLine, PaymentLine
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


def _two_line_draft(branch, owner, cashier):
    v1 = stocked_variant(
        branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
    )
    v2 = stocked_variant(branch, owner, price="500.00", quantity=20, unit_cost="250.00")
    sale = create_draft_sale(
        branch=branch,
        cashier=cashier,
        cart=[
            CartLine(variant_id=v1.id, quantity=3),  # 3000
            CartLine(variant_id=v2.id, quantity=4),  # 2000
        ],
        client_sale_id=uuid.uuid4(),
    )
    return sale, v1, v2  # subtotal 5000


class TestAllocateDiscount:
    @pytest.mark.parametrize(
        ("subtotal", "discount", "weights", "expected"),
        [
            (5000_00, 1000_00, [3000_00, 2000_00], [600_00, 400_00]),
            (300, 100, [100, 100, 100], [34, 33, 33]),  # 100/3 -> largest remainder
            (1000, 1, [700, 300], [1, 0]),
            (999_99, 333_33, [333_33, 333_33, 333_33], [111_11, 111_11, 111_11]),
        ],
    )
    def test_allocations_sum_exactly_and_are_deterministic(
        self, subtotal, discount, weights, expected
    ):
        alloc = allocate_discount(subtotal, discount, weights)
        assert sum(alloc) == discount
        assert alloc == expected
        assert allocate_discount(subtotal, discount, weights) == alloc  # deterministic


class TestCreateDraft:
    def test_draft_has_no_payment_receipt_movement(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, _v2 = _two_line_draft(branch, owner, cashier)
        assert sale.status == SaleStatus.DRAFT
        assert sale.subtotal == Decimal("5000.00")
        assert sale.discount_total == Decimal("0.00")
        assert sale.total == Decimal("5000.00")
        assert sale.receipt_number is None
        assert not sale.payments.exists()
        assert not StockMovement.objects.filter(
            variant=v1, movement_type=MovementType.SALE
        ).exists()
        assert InventoryBalance.objects.get(branch=branch, variant=v1).quantity == 20

    def test_draft_is_not_reportable(self, branch, owner):
        from apps.finance.services.reports import profit_report

        cashier = EmployeeFactory(branch=branch)
        _two_line_draft(branch, owner, cashier)
        r = profit_report(
            branch=branch, period="custom", start="2000-01-01", end="2100-01-01"
        )
        assert r["revenue"] == Decimal("0.00")
        assert r["sales_count"] == 0

    def test_draft_without_active_price_rejected(self, branch, owner):
        from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory

        cashier = EmployeeFactory(branch=branch)
        variant = ProductVariantFactory(
            product=ProductFactory(branch=branch, is_active=True)
        )
        with pytest.raises(APIError) as exc:
            create_draft_sale(
                branch=branch,
                cashier=cashier,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "price_not_set"


class TestRequestDiscount:
    def test_reason_is_required(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        with pytest.raises(APIError):
            request_discount(
                sale=sale, requested_by=cashier, amount=Decimal("500.00"), reason="  "
            )

    def test_amount_must_be_between_zero_and_subtotal(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        for bad in (
            Decimal("0.00"),
            Decimal("-1.00"),
            Decimal("5000.00"),
            Decimal("6000.00"),
        ):
            with pytest.raises(APIError):
                request_discount(
                    sale=sale, requested_by=cashier, amount=bad, reason="loyal customer"
                )

    def test_request_moves_draft_to_pending_approval(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale,
            requested_by=cashier,
            amount=Decimal("1000.00"),
            reason="bulk purchase",
        )
        assert req.request_type == ApprovalType.DISCOUNT
        assert req.status == ApprovalStatus.PENDING
        sale.refresh_from_db()
        assert sale.status == SaleStatus.PENDING_APPROVAL
        assert req.requested_changes["fingerprint"] == draft_fingerprint(sale)


class TestApproveRejectDiscount:
    def test_owner_approves_and_records_amount(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("1000.00"), reason="x"
        )
        approve_discount(
            approval=req, owner=owner, amount=Decimal("800.00"), reviewer_note="ok"
        )
        req.refresh_from_db()
        sale.refresh_from_db()
        assert req.status == ApprovalStatus.APPROVED
        assert Decimal(req.requested_changes["approved_amount"]) == Decimal("800.00")
        assert sale.discount_total == Decimal("800.00")
        assert sale.total == Decimal("4200.00")
        assert sale.status == SaleStatus.PENDING_APPROVAL

    def test_reject_has_no_side_effects_and_reverts_to_draft(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("1000.00"), reason="x"
        )
        reject_discount(approval=req, owner=owner, reviewer_note="no")
        req.refresh_from_db()
        sale.refresh_from_db()
        assert req.status == ApprovalStatus.REJECTED
        assert sale.status == SaleStatus.DRAFT
        assert sale.discount_total == Decimal("0.00")
        assert sale.total == Decimal("5000.00")

    def test_approved_amount_must_be_less_than_subtotal(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("1000.00"), reason="x"
        )
        with pytest.raises(APIError):
            approve_discount(approval=req, owner=owner, amount=Decimal("5000.00"))

    def test_cart_edit_invalidates_a_pending_request(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, _v2 = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("1000.00"), reason="x"
        )
        replace_draft_cart(sale=sale, cart=[CartLine(variant_id=v1.id, quantity=1)])
        req.refresh_from_db()
        sale.refresh_from_db()
        assert req.status == ApprovalStatus.REJECTED  # auto-superseded
        assert sale.status == SaleStatus.DRAFT
        assert sale.subtotal == Decimal("1000.00")

    def test_price_change_after_approval_blocks_finalisation(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, _v2 = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("1000.00"), reason="x"
        )
        approve_discount(approval=req, owner=owner, amount=Decimal("1000.00"))
        set_active_price(variant=v1, amount=Decimal("9999.00"), changed_by=owner)
        with pytest.raises(Conflict) as exc:
            finalise_draft(
                sale=Sale.objects.get(pk=sale.pk),
                cashier=cashier,
                payments=[
                    PaymentLine(
                        method="CASH",
                        amount=Decimal("4000.00"),
                        tendered_amount=Decimal("4000.00"),
                    )
                ],
            )
        assert exc.value.code == "approval_stale"
        assert not Sale.objects.filter(pk=sale.pk, status=SaleStatus.COMPLETED).exists()


class TestFinalise:
    def _approved_draft(self, branch, owner, cashier, approved="1000.00"):
        sale, v1, v2 = _two_line_draft(branch, owner, cashier)
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal(approved), reason="x"
        )
        approve_discount(approval=req, owner=owner, amount=Decimal(approved))
        return sale, v1, v2

    def test_finalise_applies_discount_allocates_reduces_stock_and_numbers_receipt(
        self, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, v2 = self._approved_draft(branch, owner, cashier, "1000.00")
        done = finalise_draft(
            sale=sale,
            cashier=cashier,
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("4000.00"),
                    tendered_amount=Decimal("4000.00"),
                )
            ],
        )
        assert done.status == SaleStatus.COMPLETED
        assert done.subtotal == Decimal("5000.00")
        assert done.discount_total == Decimal("1000.00")
        assert done.total == Decimal("4000.00")
        assert done.receipt_number
        # discount allocated 3000:2000 -> 600:400
        i1 = done.items.get(variant=v1)
        i2 = done.items.get(variant=v2)
        assert i1.discount_amount == Decimal("600.00")
        assert i2.discount_amount == Decimal("400.00")
        assert i1.discount_amount + i2.discount_amount == done.discount_total
        assert i1.line_total == Decimal("2400.00")  # 3000 - 600
        assert i2.line_total == Decimal("1600.00")  # 2000 - 400
        # cost snapshot taken at finalisation
        assert i1.unit_cost_snapshot == Decimal("600.00")
        assert InventoryBalance.objects.get(branch=branch, variant=v1).quantity == 17
        assert (
            StockMovement.objects.filter(
                variant=v1, movement_type=MovementType.SALE
            ).count()
            == 1
        )

    def test_payments_must_equal_subtotal_minus_discount(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = self._approved_draft(branch, owner, cashier, "1000.00")
        with pytest.raises(APIError) as exc:
            finalise_draft(
                sale=sale,
                cashier=cashier,
                payments=[
                    PaymentLine(
                        method="CASH",
                        amount=Decimal("5000.00"),
                        tendered_amount=Decimal("5000.00"),
                    )
                ],
            )
        assert exc.value.code == "payment_mismatch"
        assert not Payment.objects.exists()

    def test_split_payment_to_discounted_total(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = self._approved_draft(branch, owner, cashier, "1000.00")
        done = finalise_draft(
            sale=sale,
            cashier=cashier,
            payments=[
                PaymentLine(method="POS", amount=Decimal("2500.00"), reference="P"),
                PaymentLine(
                    method="CASH",
                    amount=Decimal("1500.00"),
                    tendered_amount=Decimal("2000.00"),
                ),
            ],
        )
        assert done.total == Decimal("4000.00")
        assert done.payments.count() == 2
        assert done.change_due == Decimal("500.00")

    def test_finalisation_is_idempotent(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, _v2 = self._approved_draft(branch, owner, cashier, "1000.00")
        payments = [
            PaymentLine(
                method="CASH",
                amount=Decimal("4000.00"),
                tendered_amount=Decimal("4000.00"),
            )
        ]
        first = finalise_draft(sale=sale, cashier=cashier, payments=payments)
        second = finalise_draft(
            sale=Sale.objects.get(pk=sale.pk), cashier=cashier, payments=payments
        )
        assert first.id == second.id
        assert Payment.objects.filter(sale=first).count() == 1
        assert (
            StockMovement.objects.filter(
                variant=v1, movement_type=MovementType.SALE
            ).count()
            == 1
        )

    def test_stock_change_before_finalisation_is_rechecked(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, v1, _v2 = self._approved_draft(branch, owner, cashier, "1000.00")
        # sell down v1's stock elsewhere so only 1 remains (draft needs 3)
        bal = lock_balances(branch, [v1.id])[v1.id]
        write_movement(
            balance=bal,
            delta=-(bal.quantity - 1),
            movement_type=MovementType.DAMAGE,
            created_by=owner,
            reason="shrinkage",
        )
        with pytest.raises(APIError) as exc:
            finalise_draft(
                sale=Sale.objects.get(pk=sale.pk),
                cashier=cashier,
                payments=[
                    PaymentLine(
                        method="CASH",
                        amount=Decimal("4000.00"),
                        tendered_amount=Decimal("4000.00"),
                    )
                ],
            )
        assert exc.value.code == "stock_not_available"

    def test_completed_sale_cannot_get_a_discount(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = self._approved_draft(branch, owner, cashier, "1000.00")
        finalise_draft(
            sale=sale,
            cashier=cashier,
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("4000.00"),
                    tendered_amount=Decimal("4000.00"),
                )
            ],
        )
        with pytest.raises((APIError, Conflict)):
            request_discount(
                sale=Sale.objects.get(pk=sale.pk),
                requested_by=cashier,
                amount=Decimal("100.00"),
                reason="too late",
            )

    def test_cancel_draft(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, *_ = _two_line_draft(branch, owner, cashier)
        cancel_draft(sale=sale, actor=cashier)
        sale.refresh_from_db()
        assert sale.status == SaleStatus.CANCELLED


class TestDiscountedReturns:
    def _finalised_discounted_sale(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        v1 = stocked_variant(
            branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
        )
        sale = create_draft_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=v1.id, quantity=5)],  # subtotal 5000
            client_sale_id=uuid.uuid4(),
        )
        req = request_discount(
            sale=sale, requested_by=cashier, amount=Decimal("500.00"), reason="x"
        )
        approve_discount(approval=req, owner=owner, amount=Decimal("500.00"))
        done = finalise_draft(
            sale=sale,
            cashier=cashier,
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("4500.00"),
                    tendered_amount=Decimal("4500.00"),
                )
            ],
        )
        return done, v1, done.items.get()

    def test_return_refunds_net_of_discount(self, branch, owner):
        from apps.sales.models import ReturnCondition
        from apps.sales.services.returns import (
            ApprovedLine,
            RefundLine,
            RequestLine,
            approve_return,
            submit_return_request,
        )

        done, _v1, item = self._finalised_discounted_sale(branch, owner)
        # net paid per unit = 4500 / 5 = 900
        req = submit_return_request(
            sale=done,
            requested_by=owner,
            reason="net refund",
            lines=[RequestLine(sale_item_id=item.id, quantity=2)],
            client_return_id=uuid.uuid4(),
        )
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
            refunds=[RefundLine(method="CASH", amount=Decimal("1800.00"))],
        )
        assert ret.total == Decimal("1800.00")  # 2 * 900, not 2 * 1000

    def test_partial_returns_never_exceed_net_and_full_return_equals_amount_paid(
        self, branch, owner
    ):
        from apps.sales.models import ReturnCondition
        from apps.sales.services.returns import (
            ApprovedLine,
            RefundLine,
            RequestLine,
            approve_return,
            submit_return_request,
        )

        done, _v1, item = self._finalised_discounted_sale(branch, owner)
        # net paid on the line = final total = 4500.00 across 5 units
        expected = {2: Decimal("1800.00"), 22: Decimal("1800.00"), 1: Decimal("900.00")}
        refunded = Decimal("0.00")
        for tag, qty in ((2, 2), (22, 2), (1, 1)):
            req = submit_return_request(
                sale=Sale.objects.get(pk=done.pk),
                requested_by=owner,
                reason="r",
                lines=[RequestLine(sale_item_id=item.id, quantity=qty)],
                client_return_id=uuid.uuid4(),
            )
            ret = approve_return(
                approval=req,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id,
                        quantity=qty,
                        condition=ReturnCondition.RESELLABLE,
                    )
                ],
                refunds=[RefundLine(method="CASH", amount=expected[tag])],
            )
            assert ret.total == expected[tag]
            refunded += ret.total
        assert refunded == Decimal("4500.00")  # exactly the final amount paid
