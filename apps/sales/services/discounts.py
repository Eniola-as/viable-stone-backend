"""Owner-approved fixed-Naira discount on an internal DRAFT sale.

A DRAFT is an internal checkout record — never a quotation. It holds no payment,
receipt, revenue, COGS, stock movement or report entry. A cashier requests an
exact discount with a reason; only the owner approves. The approval is bound to
the draft's exact contents and the server-side prices at request time; editing
the cart or changing an active price invalidates it. Finalisation re-checks
stock and prices, allocates the fixed discount across items in kobo so the parts
sum exactly, and is atomic / idempotent / concurrency-safe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.catalog.selectors import current_price
from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money
from apps.core.services.audit import record_audit
from apps.inventory.models import MovementType
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    Payment,
    Sale,
    SaleItem,
    SaleSource,
    SaleStatus,
)
from apps.sales.services.sales import (
    CartLine,
    PaymentLine,
    merge_cart,
    next_receipt_number,
    validate_payments,
)

_DRAFT_STATES = (SaleStatus.DRAFT, SaleStatus.PENDING_APPROVAL)


@dataclass
class _Line:
    variant: ProductVariant
    quantity: int
    unit_price: Decimal


# --------------------------------------------------------------------------- #
# Discount allocation                                                          #
# --------------------------------------------------------------------------- #


def allocate_discount(
    subtotal_minor: int, discount_minor: int, weights_minor: list[int]
) -> list[int]:
    """Split ``discount_minor`` across lines by ``weights_minor`` (line subtotals).

    Largest-remainder method in currency minor units — the parts always sum to
    exactly ``discount_minor``. Deterministic: ties break by original index.
    """

    total_weight = sum(weights_minor)
    if total_weight <= 0 or discount_minor <= 0:
        return [0] * len(weights_minor)
    base = []
    remainders = []
    for w in weights_minor:
        num = discount_minor * w
        base.append(num // total_weight)
        remainders.append(num % total_weight)
    leftover = discount_minor - sum(base)
    # hand the leftover minor units to the largest remainders (stable order)
    order = sorted(range(len(weights_minor)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        base[i] += 1
    return base


def _to_minor(value) -> int:
    return int((Decimal(value) * 100).to_integral_value())


# --------------------------------------------------------------------------- #
# Fingerprint                                                                  #
# --------------------------------------------------------------------------- #


def draft_fingerprint(sale: Sale) -> str:
    """Hash of the draft's lines + the *current* active price of each variant.

    Any cart edit or active-price change flips this value, so an approval bound
    to an earlier fingerprint is detected as stale.
    """

    parts = []
    for item in sale.items.select_related("variant").order_by("variant_id"):
        price = current_price(item.variant)
        parts.append(
            [
                str(item.variant_id),
                int(item.quantity),
                str(to_money(price.amount)) if price else None,
            ]
        )
    payload = json.dumps(
        {"lines": parts, "subtotal": str(sale.subtotal)}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Draft lifecycle                                                             #
# --------------------------------------------------------------------------- #


def _price_lines(branch, cart: list[CartLine]) -> list[_Line]:
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
    lines: list[_Line] = []
    for variant_id, quantity in merged.items():
        variant = variants[variant_id]
        if not (variant.is_active and variant.product.is_active):
            raise APIError(
                f"{variant.sku} is not available for sale.", code="variant_unavailable"
            )
        price = current_price(variant)
        if price is None:
            raise APIError(
                f"No active selling price is set for {variant.sku}.",
                code="price_not_set",
            )
        lines.append(
            _Line(variant=variant, quantity=quantity, unit_price=to_money(price.amount))
        )
    return lines


def _write_draft_items(sale: Sale, lines: list[_Line]) -> Decimal:
    sale.items.all().delete()
    SaleItem.objects.bulk_create(
        [
            SaleItem(
                sale=sale,
                variant=line.variant,
                product_name_snapshot=line.variant.product.name,
                sku_snapshot=line.variant.sku,
                variant_description_snapshot=line.variant.description,
                quantity=line.quantity,
                unit_price_snapshot=line.unit_price,
                unit_cost_snapshot=ZERO,  # re-snapshot at finalisation
                discount_amount=ZERO,
                line_total=to_money(line.unit_price * line.quantity),
            )
            for line in lines
        ]
    )
    return to_money(sum((line.unit_price * line.quantity for line in lines), ZERO))


@transaction.atomic
def create_draft_sale(
    *,
    branch,
    cashier,
    cart: list[CartLine],
    client_sale_id,
    customer=None,
    request=None,
) -> Sale:
    existing = Sale.objects.filter(branch=branch, client_sale_id=client_sale_id).first()
    if existing is not None:
        return existing
    if not cashier.is_active or cashier.branch_id != branch.id:
        raise APIError("Invalid cashier for this branch.", code="wrong_branch")

    lines = _price_lines(branch, cart)
    sale = Sale.objects.create(
        branch=branch,
        cashier=cashier,
        customer=customer,
        status=SaleStatus.DRAFT,
        source=SaleSource.ONLINE,
        client_sale_id=client_sale_id,
        subtotal=ZERO,
        discount_total=ZERO,
        total=ZERO,
    )
    subtotal = _write_draft_items(sale, lines)
    sale.subtotal = subtotal
    sale.total = subtotal
    sale.save(update_fields=["subtotal", "total", "updated_at"])
    record_audit(
        action="draft.create",
        target=sale,
        actor=cashier,
        branch=branch,
        request=request,
        after={"subtotal": str(subtotal), "lines": len(lines)},
    )
    return sale


def _supersede_pending_discount(sale: Sale, note: str) -> None:
    pending = ApprovalRequest.objects.filter(
        sale=sale,
        request_type=ApprovalType.DISCOUNT,
        status=ApprovalStatus.PENDING,
    ).first()
    if pending is not None:
        pending.status = ApprovalStatus.REJECTED
        pending.reviewer_note = note
        pending.reviewed_at = timezone.now()
        pending.save(
            update_fields=["status", "reviewer_note", "reviewed_at", "updated_at"]
        )


@transaction.atomic
def replace_draft_cart(*, sale, cart: list[CartLine], request=None) -> Sale:
    locked = Sale.objects.select_for_update().get(pk=sale.pk)
    if locked.status not in _DRAFT_STATES:
        raise Conflict("Only a draft cart can be edited.", code="draft_not_editable")
    lines = _price_lines(locked.branch, cart)
    subtotal = _write_draft_items(locked, lines)
    # Editing invalidates any pending discount approval (RULE 9).
    _supersede_pending_discount(locked, "Superseded: draft cart edited.")
    locked.status = SaleStatus.DRAFT
    locked.subtotal = subtotal
    locked.discount_total = ZERO
    locked.total = subtotal
    locked.save(
        update_fields=["status", "subtotal", "discount_total", "total", "updated_at"]
    )
    return locked


@transaction.atomic
def cancel_draft(*, sale, actor, request=None) -> Sale:
    locked = Sale.objects.select_for_update().get(pk=sale.pk)
    if locked.status not in _DRAFT_STATES:
        raise Conflict("Only a draft can be cancelled.", code="draft_not_cancellable")
    _supersede_pending_discount(locked, "Superseded: draft cancelled.")
    locked.status = SaleStatus.CANCELLED
    locked.save(update_fields=["status", "updated_at"])
    record_audit(
        action="draft.cancel",
        target=locked,
        actor=actor,
        branch=locked.branch,
        request=request,
    )
    return locked


# --------------------------------------------------------------------------- #
# Discount request / decision                                                 #
# --------------------------------------------------------------------------- #


def _validate_discount_amount(amount: Decimal, subtotal: Decimal) -> Decimal:
    amount = to_money(amount)
    if amount <= 0 or amount >= subtotal:
        raise APIError(
            "The discount must be greater than zero and less than the subtotal.",
            code="invalid_discount",
            field_errors={"amount": ["0 < amount < subtotal."]},
        )
    return amount


@transaction.atomic
def request_discount(
    *, sale, requested_by, amount, reason, request=None
) -> ApprovalRequest:
    reason = (reason or "").strip()
    if not reason:
        raise APIError("A reason is required.", code="reason_required")

    locked = Sale.objects.select_for_update().get(pk=sale.pk)
    if locked.status not in _DRAFT_STATES:
        raise Conflict(
            "A discount can only be requested on a draft sale.",
            code="not_a_draft",
        )
    if ApprovalRequest.objects.filter(
        sale=locked,
        request_type=ApprovalType.DISCOUNT,
        status=ApprovalStatus.PENDING,
    ).exists():
        raise Conflict(
            "A discount request is already pending for this draft.",
            code="discount_request_pending",
        )

    amount = _validate_discount_amount(amount, locked.subtotal)
    fingerprint = draft_fingerprint(locked)
    approval = ApprovalRequest.objects.create(
        branch=locked.branch,
        sale=locked,
        request_type=ApprovalType.DISCOUNT,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        reason=reason,
        requested_changes={
            "requested_amount": str(amount),
            "subtotal": str(locked.subtotal),
            "fingerprint": fingerprint,
        },
    )
    locked.status = SaleStatus.PENDING_APPROVAL
    locked.save(update_fields=["status", "updated_at"])
    record_audit(
        action="discount.request",
        target=approval,
        actor=requested_by,
        branch=locked.branch,
        request=request,
        after={"requested_amount": str(amount)},
    )
    return approval


def _locked_pending_discount(approval):
    locked = (
        ApprovalRequest.objects.select_for_update(of=("self",))
        .select_related("sale", "branch")
        .get(pk=approval.pk)
    )
    if locked.status != ApprovalStatus.PENDING:
        raise Conflict(
            f"This request is already {locked.status.lower()}.",
            code="approval_not_pending",
        )
    if locked.request_type != ApprovalType.DISCOUNT or locked.sale is None:
        raise APIError("This is not a discount request.", code="not_a_discount")
    return locked


@transaction.atomic
def approve_discount(
    *, approval, owner, amount, reviewer_note="", request=None
) -> ApprovalRequest:
    locked = _locked_pending_discount(approval)
    sale = Sale.objects.select_for_update().get(pk=locked.sale_id)
    if sale.status != SaleStatus.PENDING_APPROVAL:
        raise Conflict("The draft is no longer awaiting approval.", code="not_a_draft")
    if draft_fingerprint(sale) != locked.requested_changes.get("fingerprint"):
        raise Conflict(
            "The draft changed since the request; ask for a fresh discount.",
            code="draft_changed",
        )

    amount = _validate_discount_amount(amount, sale.subtotal)
    now = timezone.now()
    locked.status = ApprovalStatus.APPROVED
    locked.reviewed_by = owner
    locked.reviewer_note = reviewer_note or ""
    locked.reviewed_at = now
    locked.requested_changes = {
        **locked.requested_changes,
        "approved_amount": str(amount),
    }
    locked.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewer_note",
            "reviewed_at",
            "requested_changes",
            "updated_at",
        ]
    )
    sale.discount_total = amount
    sale.total = to_money(sale.subtotal - amount)
    sale.save(update_fields=["discount_total", "total", "updated_at"])
    record_audit(
        action="discount.approve",
        target=locked,
        actor=owner,
        branch=sale.branch,
        request=request,
        after={"approved_amount": str(amount)},
    )
    return locked


@transaction.atomic
def reject_discount(
    *, approval, owner, reviewer_note="", request=None
) -> ApprovalRequest:
    locked = _locked_pending_discount(approval)
    sale = Sale.objects.select_for_update().get(pk=locked.sale_id)
    locked.status = ApprovalStatus.REJECTED
    locked.reviewed_by = owner
    locked.reviewer_note = reviewer_note or ""
    locked.reviewed_at = timezone.now()
    locked.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewer_note",
            "reviewed_at",
            "updated_at",
        ]
    )
    if sale.status == SaleStatus.PENDING_APPROVAL:
        sale.status = SaleStatus.DRAFT
        sale.discount_total = ZERO
        sale.total = sale.subtotal
        sale.save(update_fields=["status", "discount_total", "total", "updated_at"])
    record_audit(
        action="discount.reject",
        target=locked,
        actor=owner,
        branch=sale.branch,
        request=request,
    )
    return locked


# --------------------------------------------------------------------------- #
# Finalisation                                                                #
# --------------------------------------------------------------------------- #


@transaction.atomic
def finalise_draft(
    *, sale, cashier, payments: list[PaymentLine], client_finalize_id=None, request=None
) -> Sale:
    locked = Sale.objects.select_for_update().select_related("branch").get(pk=sale.pk)
    if locked.status == SaleStatus.COMPLETED:
        return locked  # idempotent
    if locked.status != SaleStatus.PENDING_APPROVAL:
        raise Conflict(
            "Only an approved draft can be finalised.", code="not_finalisable"
        )

    approval = (
        ApprovalRequest.objects.filter(
            sale=locked,
            request_type=ApprovalType.DISCOUNT,
            status=ApprovalStatus.APPROVED,
        )
        .order_by("-reviewed_at")
        .first()
    )
    if approval is None:
        raise Conflict(
            "No approved discount for this draft.", code="discount_not_approved"
        )
    if draft_fingerprint(locked) != approval.requested_changes.get("fingerprint"):
        raise Conflict(
            "The cart or a price changed since approval; request a new discount.",
            code="approval_stale",
        )

    items = list(
        locked.items.select_for_update().select_related("variant").order_by("pk")
    )
    balances = lock_balances(locked.branch, [i.variant_id for i in items])

    subtotal_minor = 0
    weights_minor = []
    for item in items:
        price = current_price(item.variant)
        if price is None:
            raise APIError(
                f"No active price for {item.sku_snapshot}.", code="price_not_set"
            )
        line_price = to_money(price.amount)
        if (
            line_price != item.unit_price_snapshot
        ):  # covered by fingerprint, belt+braces
            raise Conflict("A price changed.", code="approval_stale")
        balance = balances[item.variant_id]
        if balance.quantity < item.quantity:
            raise APIError(
                f"Only {balance.quantity} unit(s) of {item.sku_snapshot} available.",
                code="stock_not_available",
                status_code=409,
            )
        weight = _to_minor(line_price) * item.quantity
        weights_minor.append(weight)
        subtotal_minor += weight

    subtotal = to_money(Decimal(subtotal_minor) / 100)
    discount = _validate_discount_amount(
        Decimal(approval.requested_changes["approved_amount"]), subtotal
    )
    discount_minor = _to_minor(discount)
    total = to_money(subtotal - discount)

    change_due = validate_payments(payments, total)

    allocations = allocate_discount(subtotal_minor, discount_minor, weights_minor)
    for item, alloc_minor in zip(items, allocations, strict=True):
        item.discount_amount = to_money(Decimal(alloc_minor) / 100)
        item.unit_cost_snapshot = to_money(balances[item.variant_id].average_unit_cost)
        gross = to_money(item.unit_price_snapshot * item.quantity)
        item.line_total = to_money(gross - item.discount_amount)
        item.save(
            update_fields=[
                "discount_amount",
                "unit_cost_snapshot",
                "line_total",
            ]
        )

    Payment.objects.bulk_create(
        [
            Payment(
                sale=locked,
                method=p.method,
                amount=to_money(p.amount),
                tendered_amount=(
                    to_money(p.tendered_amount)
                    if p.tendered_amount is not None
                    else None
                ),
                reference=p.reference or "",
            )
            for p in payments
        ]
    )
    for item in items:
        write_movement(
            balance=balances[item.variant_id],
            delta=-item.quantity,
            movement_type=MovementType.SALE,
            created_by=cashier,
            unit_cost_snapshot=item.unit_cost_snapshot,
            reference_type="sale",
            reference_id=locked.id,
        )

    locked.subtotal = subtotal
    locked.discount_total = discount
    locked.total = total
    locked.change_due = change_due
    locked.receipt_number = next_receipt_number(locked.branch)
    locked.status = SaleStatus.COMPLETED
    locked.completed_at = timezone.now()
    locked.save(
        update_fields=[
            "subtotal",
            "discount_total",
            "total",
            "change_due",
            "receipt_number",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    record_audit(
        action="draft.finalise",
        target=locked,
        actor=cashier,
        branch=locked.branch,
        request=request,
        after={
            "receipt_number": locked.receipt_number,
            "subtotal": str(subtotal),
            "discount": str(discount),
            "total": str(total),
        },
    )
    return locked
