"""Stage 14 — approved returns must flow into profit and best-seller reports."""

import uuid
from decimal import Decimal

import pytest
from freezegun import freeze_time

from apps.accounts.tests.factories import EmployeeFactory
from apps.finance.services.reports import best_sellers, profit_report
from apps.sales.models import ReturnCondition
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    RequestLine,
    approve_return,
    submit_return_request,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

RANGE = {"period": "custom", "start": "2026-08-01", "end": "2026-08-31"}


def _sell(branch, owner, variant, qty, price):
    return create_sale(
        branch=branch,
        cashier=EmployeeFactory(branch=branch),
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


def _approve(sale, owner, item, qty, condition, refund_amount):
    req = submit_return_request(
        sale=sale,
        requested_by=owner,
        reason="report netting",
        lines=[RequestLine(sale_item_id=item.id, quantity=qty)],
        client_return_id=uuid.uuid4(),
    )
    return approve_return(
        approval=req,
        owner=owner,
        lines=[ApprovedLine(sale_item_id=item.id, quantity=qty, condition=condition)],
        refunds=[RefundLine(method="CASH", amount=Decimal(refund_amount))],
    )


class TestProfitReportNetsReturns:
    def test_resellable_return_reduces_revenue_and_reverses_cogs(self, branch, owner):
        # sell 5 @ 1000 (cost 600): revenue 5000, cogs 3000, gross 2000
        with freeze_time("2026-08-10 09:00:00"):
            v = stocked_variant(
                branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
            )
            sale = _sell(branch, owner, v, 5, "1000.00")
            item = sale.items.get()
            _approve(sale, owner, item, 2, ReturnCondition.RESELLABLE, "2000.00")

        report = profit_report(branch=branch, **RANGE)
        assert report["revenue"] == Decimal("3000.00")  # 5000 - 2000
        assert report["cogs"] == Decimal("1800.00")  # 3000 - (2 * 600)
        assert report["gross_profit"] == Decimal("1200.00")
        assert report["returns_total"] == Decimal("2000.00")

    def test_damaged_return_reduces_revenue_but_keeps_cogs(self, branch, owner):
        with freeze_time("2026-08-10 09:00:00"):
            v = stocked_variant(
                branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
            )
            sale = _sell(branch, owner, v, 5, "1000.00")
            item = sale.items.get()
            _approve(sale, owner, item, 2, ReturnCondition.DAMAGED_OR_OPENED, "2000.00")

        report = profit_report(branch=branch, **RANGE)
        assert report["revenue"] == Decimal("3000.00")  # 5000 - 2000
        assert report["cogs"] == Decimal("3000.00")  # unchanged — loss retained
        assert report["gross_profit"] == Decimal("0.00")

    def test_pending_return_does_not_affect_report(self, branch, owner):
        with freeze_time("2026-08-10 09:00:00"):
            v = stocked_variant(
                branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
            )
            sale = _sell(branch, owner, v, 5, "1000.00")
            item = sale.items.get()
            submit_return_request(
                sale=sale,
                requested_by=owner,
                reason="pending only",
                lines=[RequestLine(sale_item_id=item.id, quantity=2)],
                client_return_id=uuid.uuid4(),
            )
        report = profit_report(branch=branch, **RANGE)
        assert report["revenue"] == Decimal("5000.00")
        assert report["returns_total"] == Decimal("0.00")


class TestBestSellersNetReturns:
    def test_returned_quantity_is_subtracted(self, branch, owner):
        with freeze_time("2026-08-10 09:00:00"):
            v = stocked_variant(
                branch, owner, price="100.00", quantity=100, unit_cost="50"
            )
            sale = _sell(branch, owner, v, 30, "100.00")
            item = sale.items.get()
            _approve(sale, owner, item, 12, ReturnCondition.RESELLABLE, "1200.00")

        rows = best_sellers(branch=branch, limit=5, **RANGE)
        row = next(r for r in rows if r["sku"] == v.sku)
        assert row["quantity_sold"] == 18  # 30 sold - 12 returned
