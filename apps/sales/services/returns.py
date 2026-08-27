"""Owner-approved rare returns.

Two steps: an employee or owner *submits* a return request against a COMPLETED
sale; only an owner *approves* (or *rejects*) it. A pending or rejected request
changes nothing — no stock, no totals, no payments, no reports. Approval is one
atomic, concurrency-safe, once-only transaction guarded by a ``client_return_id``
idempotency key. Approved and rejected requests are immutable. The original paid
receipt is never touched — the return has its own record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money
from apps.core.services.audit import record_audit
from apps.inventory.models import MovementType
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    PaymentMethod,
    Refund,
    ReturnCondition,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    SaleReturnStatus,
    SaleStatus,
)

_METHODS = {m.value for m in PaymentMethod}
_CONDITIONS = {c.value for c in ReturnCondition}


@dataclass
class RequestLine:
    sale_item_id: object
    quantity: int


@dataclass
class ApprovedLine:
    sale_item_id: object
    quantity: int
    condition: str


@dataclass
class RefundLine:
    method: str
    amount: Decimal
    reference: str = ""


@dataclass
class _Resolved:
    sale_item: SaleItem
    quantity: int
    condition: str
    unit_price: Decimal
    unit_cost: Decimal
    line_total: Decimal = field(default=ZERO)


def _returned_quantity(sale_item) -> int:
    return (
        SaleReturnItem.objects.filter(
            original_sale_item=sale_item,
            sale_return__status=SaleReturnStatus.COMPLETED,
        ).aggregate(q=Sum("quantity"))["q"]
        or 0
    )


def submit_return_request(
    *,
    sale,
    requested_by,
    reason,
    lines: list[RequestLine],
    client_return_id,
    request=None,
) -> ApprovalRequest:
    reason = (reason or "").strip()
    if not reason:
        raise APIError("A reason is required.", code="reason_required")
    if sale.status not in (
        SaleStatus.COMPLETED,
        SaleStatus.PARTIALLY_RETURNED,
    ):
        raise APIError(
            "Returns can only be requested against a completed sale.",
            code="sale_not_completed",
        )
    if not lines:
        raise APIError("Add at least one line to return.", code="empty_return")

    items = {i.id: i for i in sale.items.all()}
    payload_lines = []
    for line in lines:
        item = items.get(line.sale_item_id)
        if item is None:
            raise APIError(
                "A line does not belong to this sale.", code="sale_item_not_found"
            )
        if line.quantity is None or int(line.quantity) <= 0:
            raise APIError("Quantities must be positive.", code="invalid_quantity")
        # A request is only a proposal — it may not exceed what was originally
        # sold on that line. The hard "sold minus already returned" cap is
        # enforced at approval time (RETURN ITEMS rule 2).
        if int(line.quantity) > item.quantity:
            raise APIError(
                f"Only {item.quantity} unit(s) of {item.sku_snapshot} were sold "
                f"on that line.",
                code="return_quantity_exceeded",
            )
        payload_lines.append(
            {"sale_item": str(item.id), "quantity": int(line.quantity)}
        )

    approval = ApprovalRequest.objects.create(
        branch=sale.branch,
        sale=sale,
        request_type=ApprovalType.RETURN,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        reason=reason,
        requested_changes={
            "lines": payload_lines,
            "client_return_id": str(client_return_id),
        },
    )
    record_audit(
        action="return.request",
        target=approval,
        actor=requested_by,
        branch=sale.branch,
        request=request,
        after={"sale": str(sale.id), "lines": payload_lines},
    )
    return approval


@transaction.atomic
def reject_return(
    *, approval, owner, reviewer_note="", request=None
) -> ApprovalRequest:
    locked = ApprovalRequest.objects.select_for_update().get(pk=approval.pk)
    if locked.status != ApprovalStatus.PENDING:
        raise Conflict(
            f"This request is already {locked.status.lower()}.",
            code="approval_not_pending",
        )
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
    record_audit(
        action="return.reject",
        target=locked,
        actor=owner,
        branch=locked.branch,
        request=request,
        after={"reviewer_note": reviewer_note or ""},
    )
    return locked


def _validate_refunds(refunds: list[RefundLine], total: Decimal) -> None:
    if not refunds:
        raise APIError("At least one refund is required.", code="refund_required")
    running = ZERO
    for r in refunds:
        if r.method not in _METHODS:
            raise APIError(
                f"Unknown refund method '{r.method}'.", code="invalid_refund_method"
            )
        amount = to_money(r.amount)
        if amount <= 0:
            raise APIError(
                "Refund amounts must be positive.", code="invalid_refund_amount"
            )
        running += amount
    if running != total:
        raise APIError(
            f"Refunds total {running}; the return total is {total}.",
            code="refund_mismatch",
        )


@transaction.atomic
def approve_return(
    *,
    approval,
    owner,
    lines: list[ApprovedLine],
    refunds: list[RefundLine],
    reviewer_note: str = "",
    client_return_id=None,
    request=None,
) -> SaleReturn:
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
    if locked.request_type != ApprovalType.RETURN or locked.sale is None:
        raise APIError("This is not a return request.", code="not_a_return")

    key = client_return_id or locked.requested_changes.get("client_return_id")
    if key is None:
        raise APIError(
            "A client_return_id is required.", code="client_return_id_required"
        )

    existing = SaleReturn.objects.filter(
        branch=locked.branch, client_return_id=key
    ).first()
    if existing is not None:
        return existing

    sale = locked.sale
    sale_items = {i.id: i for i in sale.items.select_for_update().order_by("pk")}
    if not lines:
        raise APIError("Approve at least one line.", code="empty_return")

    resolved: list[_Resolved] = []
    for line in lines:
        try:
            sale_item = sale_items[_uuid_key(line.sale_item_id, sale_items)]
        except KeyError:
            raise APIError(
                "A line does not belong to this sale.", code="sale_item_not_found"
            ) from None
        if line.condition not in _CONDITIONS:
            raise APIError(
                f"Unknown condition '{line.condition}'.", code="invalid_condition"
            )
        qty = int(line.quantity)
        if qty <= 0:
            raise APIError("Quantities must be positive.", code="invalid_quantity")
        remaining = sale_item.quantity - _returned_quantity(sale_item)
        if qty > remaining:
            raise APIError(
                f"Only {remaining} unit(s) of {sale_item.sku_snapshot} can still "
                f"be returned.",
                code="return_quantity_exceeded",
            )
        line_total = to_money(sale_item.unit_price_snapshot * qty)
        resolved.append(
            _Resolved(
                sale_item=sale_item,
                quantity=qty,
                condition=line.condition,
                unit_price=to_money(sale_item.unit_price_snapshot),
                unit_cost=to_money(sale_item.unit_cost_snapshot),
                line_total=line_total,
            )
        )

    return_total = to_money(sum((r.line_total for r in resolved), ZERO))
    _validate_refunds(refunds, return_total)

    now = timezone.now()
    try:
        sale_return = SaleReturn.objects.create(
            branch=locked.branch,
            original_sale=sale,
            approval=locked,
            status=SaleReturnStatus.COMPLETED,
            total=return_total,
            client_return_id=key,
            approved_by=owner,
            completed_at=now,
        )
    except IntegrityError as err:
        raise Conflict(
            "A return with this reference already exists.",
            code="return_in_progress",
        ) from err

    SaleReturnItem.objects.bulk_create(
        [
            SaleReturnItem(
                sale_return=sale_return,
                original_sale_item=r.sale_item,
                quantity=r.quantity,
                condition=r.condition,
                unit_price_snapshot=r.unit_price,
                unit_cost_snapshot=r.unit_cost,
                line_total=r.line_total,
            )
            for r in resolved
        ]
    )
    Refund.objects.bulk_create(
        [
            Refund(
                sale_return=sale_return,
                method=r.method,
                amount=to_money(r.amount),
                reference=r.reference or "",
                issued_by=owner,
            )
            for r in refunds
        ]
    )

    # Restore inventory only for RESELLABLE lines, deterministic lock order.
    resellable = [r for r in resolved if r.condition == ReturnCondition.RESELLABLE]
    if resellable:
        balances = lock_balances(
            locked.branch, [r.sale_item.variant_id for r in resellable]
        )
        for r in resellable:
            write_movement(
                balance=balances[r.sale_item.variant_id],
                delta=r.quantity,
                movement_type=MovementType.RETURN,
                created_by=owner,
                unit_cost_snapshot=r.unit_cost,
                reference_type="sale_return",
                reference_id=sale_return.id,
                reason=f"Return approved ({locked.reason[:120]})",
            )

    # Sale status: fully returned vs partially.
    sold = sum(i.quantity for i in sale.items.all())
    returned = (
        SaleReturnItem.objects.filter(
            original_sale_item__sale=sale,
            sale_return__status=SaleReturnStatus.COMPLETED,
        ).aggregate(q=Sum("quantity"))["q"]
        or 0
    )
    sale.status = (
        SaleStatus.RETURNED if returned >= sold else SaleStatus.PARTIALLY_RETURNED
    )
    sale.save(update_fields=["status", "updated_at"], force=True)

    locked.status = ApprovalStatus.APPROVED
    locked.reviewed_by = owner
    locked.reviewer_note = reviewer_note or ""
    locked.reviewed_at = now
    locked.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewer_note",
            "reviewed_at",
            "updated_at",
        ]
    )

    record_audit(
        action="return.approve",
        target=sale_return,
        actor=owner,
        branch=locked.branch,
        request=request,
        after={
            "sale": str(sale.id),
            "total": str(return_total),
            "lines": len(resolved),
            "resellable_lines": len(resellable),
            "sale_status": sale.status,
        },
    )
    return sale_return


def _uuid_key(value, mapping):
    """Match a str/UUID line id against the sale_items dict keys."""

    for key in mapping:
        if str(key) == str(value):
            return key
    raise KeyError(value)
