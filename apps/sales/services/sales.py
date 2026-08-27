"""``create_sale`` — the protected, idempotent, fully-paid checkout workflow.

Every rule the blueprint lists for a sale lives here, inside one
``transaction.atomic()``: active cashier/branch, merged cart lines, deterministic
inventory locking, server-side prices, stock and payment validation, snapshots,
one SALE movement per variant, a safe receipt number, and an idempotency key so a
retry never duplicates a sale or a stock reduction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.catalog.selectors import current_price
from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money
from apps.core.services.audit import record_audit
from apps.inventory.models import MovementType
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    Payment,
    PaymentMethod,
    ReceiptSequence,
    Sale,
    SaleItem,
    SaleSource,
    SaleStatus,
)

_VALID_METHODS = {m.value for m in PaymentMethod}


@dataclass
class CartLine:
    variant_id: object
    quantity: int
    unit_price: Decimal | None = None  # accepted from clients but ignored


@dataclass
class PaymentLine:
    method: str
    amount: Decimal
    tendered_amount: Decimal | None = None
    reference: str = ""


@dataclass
class _Priced:
    variant: ProductVariant
    quantity: int
    unit_price: Decimal
    unit_cost: Decimal
    balance: object = field(default=None)


def merge_cart(cart: list[CartLine]) -> dict:
    if not cart:
        raise APIError("The cart is empty.", code="empty_cart")
    merged: dict = {}
    for line in cart:
        if line.quantity is None or int(line.quantity) <= 0:
            raise APIError(
                "Every quantity must be a positive whole number.",
                code="invalid_quantity",
                field_errors={"quantity": ["Must be greater than zero."]},
            )
        merged[line.variant_id] = merged.get(line.variant_id, 0) + int(line.quantity)
    return merged


def validate_payments(payments: list[PaymentLine], total: Decimal) -> Decimal:
    if not payments:
        raise APIError("At least one payment is required.", code="payment_required")
    running = ZERO
    change_due = ZERO
    for pay in payments:
        if pay.method not in _VALID_METHODS:
            raise APIError(
                f"Unknown payment method '{pay.method}'.",
                code="invalid_payment_method",
            )
        amount = to_money(pay.amount)
        if amount <= 0:
            raise APIError(
                "Every payment amount must be greater than zero.",
                code="invalid_payment_amount",
            )
        if pay.tendered_amount is not None:
            if pay.method != PaymentMethod.CASH:
                raise APIError(
                    "A tendered amount is only valid for a cash payment.",
                    code="invalid_tendered_amount",
                )
            tendered = to_money(pay.tendered_amount)
            if tendered < amount:
                raise APIError(
                    "The cash tendered is less than the cash amount.",
                    code="invalid_tendered_amount",
                )
            change_due += tendered - amount
        running += amount
    if running != total:
        raise APIError(
            f"Payments total {running}; the sale total is {total}.",
            code="payment_mismatch",
        )
    return to_money(change_due)


def next_receipt_number(branch, business_date=None) -> str:
    business_date = business_date or timezone.localdate()
    sequence, _ = ReceiptSequence.objects.select_for_update().get_or_create(
        branch=branch, business_date=business_date, defaults={"last_number": 0}
    )
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"{branch.receipt_prefix}-{business_date:%Y%m%d}-{sequence.last_number:04d}"


def _existing_for_key(branch, client_sale_id):
    return (
        Sale.objects.filter(branch=branch, client_sale_id=client_sale_id)
        .prefetch_related("items", "payments")
        .first()
    )


@transaction.atomic
def create_sale(
    *,
    branch,
    cashier,
    cart: list[CartLine],
    payments: list[PaymentLine],
    client_sale_id,
    customer=None,
    source: str = SaleSource.ONLINE,
    approved_discount: Decimal = ZERO,
    client_total: Decimal | None = None,  # accepted, never trusted
    fixed_prices: dict | None = None,  # {str(variant_id): price} from a signed snapshot
    completed_at=None,  # true sale time (offline sales keep their original time)
    business_date=None,  # receipt-number day (defaults to today)
    payments_confirmed_offline: bool = False,
    request=None,
) -> Sale:
    # --- Idempotency ---------------------------------------------------- #
    existing = _existing_for_key(branch, client_sale_id)
    if existing is not None:
        if existing.status == SaleStatus.COMPLETED:
            return existing
        raise Conflict(
            "A sale with this reference is already in progress.",
            code="sale_in_progress",
        )

    # --- Exclusive offline session --------------------------------- #
    # An online sale cannot run while the branch's offline till is authorised.
    # The offline sync path itself passes source=OFFLINE and is exempt.
    if source != SaleSource.OFFLINE:
        from apps.accounts.services.offline_session import (
            assert_no_active_offline_session,
        )

        assert_no_active_offline_session(branch)

    # --- Cashier / branch -------------------------------------------- #
    if not cashier.is_active:
        raise APIError("This cashier account is disabled.", code="cashier_inactive")
    if cashier.branch_id != branch.id:
        raise APIError(
            "This cashier does not belong to this branch.", code="wrong_branch"
        )

    # --- Cart --------------------------------------------------------- #
    merged = merge_cart(cart)
    variant_ids = list(merged)
    variants = {
        v.id: v
        for v in ProductVariant.objects.select_related("product").filter(
            id__in=variant_ids, product__branch=branch
        )
    }
    if len(variants) != len(variant_ids):
        raise APIError(
            "One or more items were not found for this branch.",
            code="variant_not_found",
        )

    balances = lock_balances(branch, variant_ids)

    priced: list[_Priced] = []
    for variant_id, quantity in merged.items():
        variant = variants[variant_id]
        if not (variant.is_active and variant.product.is_active):
            raise APIError(
                f"{variant.sku} is not available for sale.",
                code="variant_unavailable",
            )
        if fixed_prices is not None:
            # Offline sync: prices are fixed by the authorised signed snapshot
            # and never recomputed from the live catalogue.
            snapshot_price = fixed_prices.get(str(variant_id))
            if snapshot_price is None:
                raise APIError(
                    f"{variant.sku} is not in the authorised catalogue snapshot.",
                    code="price_not_in_snapshot",
                )
            unit_price = to_money(snapshot_price)
        else:
            price_row = current_price(variant)
            if price_row is None:
                raise APIError(
                    f"No active selling price is set for {variant.sku}.",
                    code="price_not_set",
                )
            unit_price = to_money(price_row.amount)
        balance = balances[variant_id]
        if balance.quantity < quantity:
            raise APIError(
                f"Only {balance.quantity} unit(s) of {variant.sku} are available.",
                code="stock_not_available",
                status_code=409,
            )
        priced.append(
            _Priced(
                variant=variant,
                quantity=quantity,
                unit_price=unit_price,
                unit_cost=to_money(balance.average_unit_cost),
                balance=balance,
            )
        )

    # --- Money ------------------------------------------------------- #
    subtotal = to_money(sum((p.unit_price * p.quantity for p in priced), ZERO))
    discount_total = to_money(approved_discount)
    if discount_total > subtotal:
        raise APIError(
            "The discount cannot exceed the subtotal.", code="invalid_discount"
        )
    total = to_money(subtotal - discount_total)
    change_due = validate_payments(payments, total)

    # --- Persist --------------------------------------------------- #
    # A savepoint so a lost idempotency-key race doesn't poison the whole
    # transaction — we roll back to here and report "in progress".
    try:
        with transaction.atomic():
            sale = Sale.objects.create(
                branch=branch,
                cashier=cashier,
                customer=customer,
                status=SaleStatus.DRAFT,
                source=source,
                client_sale_id=client_sale_id,
                subtotal=subtotal,
                discount_total=discount_total,
                total=total,
                change_due=ZERO,
            )
    except IntegrityError as err:
        raise Conflict(
            "A sale with this reference is already in progress. Retry shortly.",
            code="sale_in_progress",
        ) from err

    SaleItem.objects.bulk_create(
        [
            SaleItem(
                sale=sale,
                variant=p.variant,
                product_name_snapshot=p.variant.product.name,
                sku_snapshot=p.variant.sku,
                variant_description_snapshot=p.variant.description,
                quantity=p.quantity,
                unit_price_snapshot=p.unit_price,
                unit_cost_snapshot=p.unit_cost,
                discount_amount=ZERO,
                line_total=to_money(p.unit_price * p.quantity),
            )
            for p in priced
        ]
    )
    Payment.objects.bulk_create(
        [
            Payment(
                sale=sale,
                method=pay.method,
                amount=to_money(pay.amount),
                tendered_amount=(
                    to_money(pay.tendered_amount)
                    if pay.tendered_amount is not None
                    else None
                ),
                reference=pay.reference or "",
                offline_confirmed=payments_confirmed_offline,
            )
            for pay in payments
        ]
    )

    for p in priced:
        write_movement(
            balance=p.balance,
            delta=-p.quantity,
            movement_type=MovementType.SALE,
            created_by=cashier,
            unit_cost_snapshot=p.unit_cost,
            reference_type="sale",
            reference_id=sale.id,
        )

    sale.receipt_number = next_receipt_number(branch, business_date=business_date)
    sale.change_due = change_due
    sale.status = SaleStatus.COMPLETED
    sale.completed_at = completed_at or timezone.now()
    sale.save(
        update_fields=[
            "receipt_number",
            "change_due",
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    record_audit(
        action="sale.create",
        target=sale,
        actor=cashier,
        branch=branch,
        request=request,
        after={
            "receipt_number": sale.receipt_number,
            "total": str(sale.total),
            "items": len(priced),
            "source": source,
        },
    )
    return sale
