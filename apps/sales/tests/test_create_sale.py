"""Business-rule tests for the create_sale checkout service (Stage 11)."""

import uuid
from decimal import Decimal

import pytest

from apps.core.exceptions import APIError
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.sales.models import Payment, Sale, SaleItem, SaleStatus
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

from .factories import stocked_variant

pytestmark = pytest.mark.django_db


def _cash(amount):
    return [
        PaymentLine(
            method="CASH", amount=Decimal(amount), tendered_amount=Decimal(amount)
        )
    ]


class TestHappyPath:
    def test_single_line_cash_sale_completes_and_reduces_stock(
        self, branch, employee, owner
    ):
        variant = stocked_variant(
            branch, owner, price="1500.00", quantity=10, unit_cost="900.00"
        )
        sale = create_sale(
            branch=branch,
            cashier=employee,
            cart=[CartLine(variant_id=variant.id, quantity=2)],
            payments=_cash("3000.00"),
            client_sale_id=uuid.uuid4(),
        )
        assert sale.status == SaleStatus.COMPLETED
        assert sale.subtotal == Decimal("3000.00")
        assert sale.discount_total == Decimal("0.00")
        assert sale.total == Decimal("3000.00")
        assert sale.completed_at is not None
        assert sale.receipt_number

        item = sale.items.get()
        assert item.quantity == 2
        assert item.unit_price_snapshot == Decimal("1500.00")
        assert item.unit_cost_snapshot == Decimal("900.00")  # weighted cost snapshot
        assert item.line_total == Decimal("3000.00")
        assert item.product_name_snapshot == variant.product.name
        assert item.sku_snapshot == variant.sku

        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 8
        )
        movement = StockMovement.objects.get(
            variant=variant, movement_type=MovementType.SALE
        )
        assert movement.quantity_delta == -2
        assert movement.reference_id == sale.id

    def test_server_ignores_any_client_supplied_price_or_total(
        self, branch, employee, owner
    ):
        variant = stocked_variant(branch, owner, price="1500.00", quantity=5)
        sale = create_sale(
            branch=branch,
            cashier=employee,
            cart=[
                CartLine(variant_id=variant.id, quantity=1, unit_price=Decimal("1.00"))
            ],
            payments=_cash("1500.00"),
            client_sale_id=uuid.uuid4(),
            client_total=Decimal("1.00"),
        )
        assert sale.total == Decimal("1500.00")

    def test_duplicate_cart_lines_are_merged(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        sale = create_sale(
            branch=branch,
            cashier=employee,
            cart=[
                CartLine(variant_id=variant.id, quantity=2),
                CartLine(variant_id=variant.id, quantity=3),
            ],
            payments=_cash("5000.00"),
            client_sale_id=uuid.uuid4(),
        )
        assert sale.items.count() == 1
        assert sale.items.get().quantity == 5
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 5
        )


class TestPayments:
    def test_split_payment_must_equal_total(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        sale = create_sale(
            branch=branch,
            cashier=employee,
            cart=[CartLine(variant_id=variant.id, quantity=3)],
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("2000.00"),
                    tendered_amount=Decimal("2000.00"),
                ),
                PaymentLine(
                    method="TRANSFER", amount=Decimal("1000.00"), reference="TRX-1"
                ),
            ],
            client_sale_id=uuid.uuid4(),
        )
        assert sale.total == Decimal("3000.00")
        assert sale.payments.count() == 2
        assert {p.method for p in sale.payments.all()} == {"CASH", "TRANSFER"}

    def test_underpaid_sale_is_rejected_with_no_records(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=3)],
                payments=_cash("2999.99"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "payment_mismatch"
        assert not Sale.objects.exists()
        assert not Payment.objects.exists()
        assert not SaleItem.objects.exists()
        assert not StockMovement.objects.filter(
            movement_type=MovementType.SALE
        ).exists()

    def test_overpaid_sale_is_rejected(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("1000.01"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "payment_mismatch"

    def test_cash_change_due_is_tendered_minus_cash_amount(
        self, branch, employee, owner
    ):
        variant = stocked_variant(branch, owner, price="1500.00", quantity=5)
        sale = create_sale(
            branch=branch,
            cashier=employee,
            cart=[CartLine(variant_id=variant.id, quantity=1)],
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("1500.00"),
                    tendered_amount=Decimal("2000.00"),
                )
            ],
            client_sale_id=uuid.uuid4(),
        )
        assert sale.change_due == Decimal("500.00")

    def test_tendered_below_cash_amount_is_rejected(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=[
                    PaymentLine(
                        method="CASH",
                        amount=Decimal("1000.00"),
                        tendered_amount=Decimal("900.00"),
                    )
                ],
                client_sale_id=uuid.uuid4(),
            )

    def test_tendered_amount_only_allowed_for_cash(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=[
                    PaymentLine(
                        method="POS",
                        amount=Decimal("1000.00"),
                        tendered_amount=Decimal("1000.00"),
                    )
                ],
                client_sale_id=uuid.uuid4(),
            )

    def test_nonpositive_payment_rejected(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=[PaymentLine(method="CASH", amount=Decimal("0.00"))],
                client_sale_id=uuid.uuid4(),
            )


class TestCartValidation:
    def test_empty_cart_rejected(self, branch, employee, owner):
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[],
                payments=_cash("0.00"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "empty_cart"

    def test_nonpositive_quantity_rejected(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=0)],
                payments=_cash("0.00"),
                client_sale_id=uuid.uuid4(),
            )

    def test_insufficient_stock_rejected_without_partial_records(
        self, branch, employee, owner
    ):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=2)
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=5)],
                payments=_cash("5000.00"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "stock_not_available"
        assert not Sale.objects.exists()
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 2
        )

    def test_inactive_variant_rejected(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        variant.is_active = False
        variant.save(update_fields=["is_active"])
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("1000.00"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code in {"variant_unavailable", "variant_not_found"}

    def test_wrong_branch_variant_rejected(self, branch, employee, owner):
        from apps.accounts.tests.factories import BranchFactory, OwnerFactory

        other_branch = BranchFactory(code="VS70")
        other_owner = OwnerFactory(branch=other_branch)
        variant = stocked_variant(
            other_branch, other_owner, price="1000.00", quantity=5
        )
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("1000.00"),
                client_sale_id=uuid.uuid4(),
            )

    def test_variant_without_active_price_rejected(self, branch, employee, owner):
        from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory

        variant = ProductVariantFactory(
            product=ProductFactory(branch=branch, is_active=True)
        )
        with pytest.raises(APIError) as exc:
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("0.00"),
                client_sale_id=uuid.uuid4(),
            )
        assert exc.value.code == "price_not_set"


class TestIdempotency:
    def test_same_client_sale_id_returns_original_and_reduces_stock_once(
        self, branch, employee, owner
    ):
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        key = uuid.uuid4()
        first = create_sale(
            branch=branch,
            cashier=employee,
            cart=[CartLine(variant_id=variant.id, quantity=3)],
            payments=_cash("3000.00"),
            client_sale_id=key,
        )
        second = create_sale(
            branch=branch,
            cashier=employee,
            cart=[CartLine(variant_id=variant.id, quantity=3)],
            payments=_cash("3000.00"),
            client_sale_id=key,
        )
        assert first.id == second.id
        assert Sale.objects.count() == 1
        assert (
            InventoryBalance.objects.get(branch=branch, variant=variant).quantity == 7
        )
        assert (
            StockMovement.objects.filter(
                variant=variant, movement_type=MovementType.SALE
            ).count()
            == 1
        )


class TestReceiptNumbering:
    def test_receipt_numbers_are_sequential_per_branch_per_day(
        self, branch, employee, owner
    ):
        variant = stocked_variant(branch, owner, price="100.00", quantity=50)
        numbers = [
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("100.00"),
                client_sale_id=uuid.uuid4(),
            ).receipt_number
            for _ in range(3)
        ]
        assert len(set(numbers)) == 3
        seqs = [int(n.rsplit("-", 1)[1]) for n in numbers]
        assert seqs == [1, 2, 3]
        assert all(n.startswith(branch.receipt_prefix) for n in numbers)

    def test_inactive_cashier_cannot_sell(self, branch, employee, owner):
        variant = stocked_variant(branch, owner, price="100.00", quantity=5)
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        with pytest.raises(APIError):
            create_sale(
                branch=branch,
                cashier=employee,
                cart=[CartLine(variant_id=variant.id, quantity=1)],
                payments=_cash("100.00"),
                client_sale_id=uuid.uuid4(),
            )
