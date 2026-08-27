"""Computed reports — revenue, COGS, profit, best/slow sellers.

Nothing here is stored. Revenue and COGS come from completed sales and the
sale-time snapshots; expenses come from non-voided ``Expense`` rows. All money is
Decimal. (Stage 14 will net approved returns out of revenue and COGS.)
"""

from __future__ import annotations

import datetime as dt

from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone

from apps.core.exceptions import APIError
from apps.core.money import ZERO, to_money
from apps.inventory.models import InventoryBalance
from apps.sales.models import (
    ReturnCondition,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    SaleReturnStatus,
    SaleStatus,
)

_NAMED = {"today", "week", "month", "custom"}
# A sale keeps its recognised revenue even after it is returned; the return is a
# separate contra entry.
_SOLD_STATUSES = (
    SaleStatus.COMPLETED,
    SaleStatus.PARTIALLY_RETURNED,
    SaleStatus.RETURNED,
)
_LINE_COGS = ExpressionWrapper(
    F("quantity") * F("unit_cost_snapshot"),
    output_field=DecimalField(max_digits=18, decimal_places=2),
)
_LINE_REVENUE = ExpressionWrapper(
    F("line_total"),
    output_field=DecimalField(max_digits=18, decimal_places=2),
)
_RET_LINE_COST = ExpressionWrapper(
    F("quantity") * F("unit_cost_snapshot"),
    output_field=DecimalField(max_digits=18, decimal_places=2),
)


def _lagos_midnight(date: dt.date):
    return timezone.make_aware(
        dt.datetime.combine(date, dt.time.min), timezone.get_current_timezone()
    )


def resolve_range(period: str, start=None, end=None):
    """Return ``(start_dt, end_dt)`` as Africa/Lagos-aware, end-*exclusive* bounds."""

    if period not in _NAMED:
        raise APIError(
            f"Unknown period '{period}'.",
            code="invalid_period",
            field_errors={"period": [f"Choose one of {sorted(_NAMED)}."]},
        )

    today = timezone.localdate()
    if period == "today":
        start_date, end_date = today, today + dt.timedelta(days=1)
    elif period == "week":
        monday = today - dt.timedelta(days=today.weekday())
        start_date, end_date = monday, monday + dt.timedelta(days=7)
    elif period == "month":
        first = today.replace(day=1)
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
        start_date, end_date = first, nxt
    else:  # custom
        try:
            start_date = dt.date.fromisoformat(str(start))
            end_date_inclusive = dt.date.fromisoformat(str(end))
        except (TypeError, ValueError) as exc:
            raise APIError(
                "start and end must be ISO dates (YYYY-MM-DD).",
                code="invalid_range",
                field_errors={
                    "start": ["ISO date required."],
                    "end": ["ISO date required."],
                },
            ) from exc
        if end_date_inclusive < start_date:
            raise APIError(
                "end must be on or after start.",
                code="invalid_range",
                field_errors={"end": ["Must be on or after start."]},
            )
        end_date = end_date_inclusive + dt.timedelta(days=1)  # make end-exclusive

    return _lagos_midnight(start_date), _lagos_midnight(end_date)


def _sales_in_period(branch, start_dt, end_dt):
    return Sale.objects.filter(
        branch=branch,
        status__in=_SOLD_STATUSES,
        completed_at__gte=start_dt,
        completed_at__lt=end_dt,
    )


def _approved_returns_in_period(branch, start_dt, end_dt):
    return SaleReturn.objects.filter(
        branch=branch,
        status=SaleReturnStatus.COMPLETED,
        completed_at__gte=start_dt,
        completed_at__lt=end_dt,
    )


def profit_report(*, branch, period="today", start=None, end=None) -> dict:
    start_dt, end_dt = resolve_range(period, start, end)
    sales = _sales_in_period(branch, start_dt, end_dt)

    revenue_gross = to_money(sales.aggregate(v=Sum("total"))["v"] or ZERO)
    sales_count = sales.count()
    cogs_gross = to_money(
        SaleItem.objects.filter(sale__in=sales).aggregate(v=Sum(_LINE_COGS))["v"]
        or ZERO
    )

    returns = _approved_returns_in_period(branch, start_dt, end_dt)
    returns_total = to_money(returns.aggregate(v=Sum("total"))["v"] or ZERO)
    # Reverse COGS only for RESELLABLE lines — DAMAGED_OR_OPENED goods are a loss.
    cost_reversed = to_money(
        SaleReturnItem.objects.filter(
            sale_return__in=returns, condition=ReturnCondition.RESELLABLE
        ).aggregate(v=Sum(_RET_LINE_COST))["v"]
        or ZERO
    )

    from apps.finance.models import Expense

    expenses_total = to_money(
        Expense.objects.filter(
            branch=branch,
            is_voided=False,
            expense_date__gte=start_dt.date(),
            expense_date__lt=end_dt.date(),
        ).aggregate(v=Sum("amount"))["v"]
        or ZERO
    )

    revenue = to_money(revenue_gross - returns_total)
    cogs = to_money(cogs_gross - cost_reversed)
    gross = to_money(revenue - cogs)
    net = to_money(gross - expenses_total)
    return {
        "period": period,
        "start": start_dt.date().isoformat(),
        "end": (end_dt.date() - dt.timedelta(days=1)).isoformat(),  # inclusive
        "currency": "NGN",
        "revenue": revenue,
        "cogs": cogs,
        "gross_profit": gross,
        "returns_total": returns_total,
        "cost_reversed": cost_reversed,
        "expenses_total": expenses_total,
        "net_profit": net,
        "sales_count": sales_count,
    }


def best_sellers(
    *, branch, period="month", start=None, end=None, limit=10
) -> list[dict]:
    start_dt, end_dt = resolve_range(period, start, end)
    sold = {
        r["variant_id"]: r
        for r in SaleItem.objects.filter(
            sale__in=_sales_in_period(branch, start_dt, end_dt)
        )
        .values("variant_id", "sku_snapshot", "product_name_snapshot")
        .annotate(quantity_sold=Sum("quantity"), revenue=Sum(_LINE_REVENUE))
    }
    # Net approved returns (both conditions — a returned unit was not truly sold).
    returned = {
        r["original_sale_item__variant_id"]: r
        for r in SaleReturnItem.objects.filter(
            sale_return__in=_approved_returns_in_period(branch, start_dt, end_dt)
        )
        .values("original_sale_item__variant_id")
        .annotate(qty=Sum("quantity"), amount=Sum("line_total"))
    }
    merged = []
    for variant_id, row in sold.items():
        ret = returned.get(variant_id, {})
        qty = row["quantity_sold"] - (ret.get("qty") or 0)
        if qty <= 0:
            continue
        merged.append(
            {
                "variant": variant_id,
                "sku": row["sku_snapshot"],
                "product_name": row["product_name_snapshot"],
                "quantity_sold": qty,
                "revenue": to_money(
                    (row["revenue"] or ZERO) - (ret.get("amount") or ZERO)
                ),
            }
        )
    merged.sort(key=lambda r: (-r["quantity_sold"], r["sku"]))
    return merged[:limit]


def slow_movers(
    *, branch, period="month", start=None, end=None, limit=20
) -> list[dict]:
    start_dt, end_dt = resolve_range(period, start, end)
    sold_variant_ids = set(
        SaleItem.objects.filter(sale__in=_sales_in_period(branch, start_dt, end_dt))
        .values_list("variant_id", flat=True)
        .distinct()
    )
    balances = (
        InventoryBalance.objects.select_related("variant", "variant__product")
        .filter(branch=branch, quantity__gt=0, variant__is_active=True)
        .exclude(variant_id__in=sold_variant_ids)
        .order_by("variant__sku")[:limit]
    )
    return [
        {
            "variant": b.variant_id,
            "sku": b.variant.sku,
            "product_name": b.variant.product.name,
            "quantity_in_stock": b.quantity,
            "quantity_sold": 0,
        }
        for b in balances
    ]
