"""Profit / revenue / COGS reports and inventory summaries (Stage 13)."""

import datetime
import uuid
from decimal import Decimal

import pytest
from freezegun import freeze_time

from apps.accounts.tests.factories import EmployeeFactory
from apps.finance.services.reports import (
    best_sellers,
    profit_report,
    resolve_range,
    slow_movers,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

from .factories import ExpenseFactory

pytestmark = pytest.mark.django_db


def _sell(branch, cashier, owner, variant, qty, unit_price):
    return create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=qty)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal(unit_price) * qty,
                tendered_amount=Decimal(unit_price) * qty,
            )
        ],
        client_sale_id=uuid.uuid4(),
    )


class TestResolveRange:
    def test_named_ranges_are_lagos_local(self):
        with freeze_time("2026-08-15 23:30:00"):  # 15 Aug 22:30 UTC -> 16 Aug Lagos
            start, end = resolve_range("today")
        assert start.date() == datetime.date(2026, 8, 16)
        assert (end - start).days == 1

    def test_custom_range(self):
        start, end = resolve_range("custom", "2026-08-01", "2026-08-07")
        assert start.date() == datetime.date(2026, 8, 1)
        assert end.date() == datetime.date(2026, 8, 8)  # end-exclusive


class TestProfitReport:
    def test_revenue_cogs_gross_and_net(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        # cost 600, sell 1000, qty 3  -> revenue 3000, cogs 1800, gross 1200
        v1 = stocked_variant(
            branch, owner, price="1000.00", quantity=10, unit_cost="600.00"
        )
        # cost 250, sell 400, qty 5   -> revenue 2000, cogs 1250, gross 750
        v2 = stocked_variant(
            branch, owner, price="400.00", quantity=10, unit_cost="250.00"
        )
        with freeze_time("2026-08-10 09:00:00"):
            _sell(branch, cashier, owner, v1, 3, "1000.00")
            _sell(branch, cashier, owner, v2, 5, "400.00")
            ExpenseFactory(
                branch=branch,
                amount=Decimal("500.00"),
                expense_date=datetime.date(2026, 8, 10),
            )
            voided = ExpenseFactory(
                branch=branch,
                amount=Decimal("9999.00"),
                expense_date=datetime.date(2026, 8, 10),
            )
        from apps.finance.services.expenses import void_expense

        void_expense(expense=voided, actor=owner, reason="mistake")

        report = profit_report(
            branch=branch, period="custom", start="2026-08-01", end="2026-08-31"
        )
        assert report["revenue"] == Decimal("5000.00")
        assert report["cogs"] == Decimal("3050.00")
        assert report["gross_profit"] == Decimal("1950.00")
        assert report["expenses_total"] == Decimal("500.00")  # voided excluded
        assert report["net_profit"] == Decimal("1450.00")
        assert report["sales_count"] == 2

    def test_sales_outside_range_are_excluded(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        v = stocked_variant(
            branch, owner, price="1000.00", quantity=10, unit_cost="600.00"
        )
        with freeze_time("2026-07-31 12:00:00"):
            _sell(branch, cashier, owner, v, 1, "1000.00")
        with freeze_time("2026-08-05 12:00:00"):
            _sell(branch, cashier, owner, v, 2, "1000.00")
        report = profit_report(
            branch=branch, period="custom", start="2026-08-01", end="2026-08-31"
        )
        assert report["revenue"] == Decimal("2000.00")
        assert report["sales_count"] == 1

    def test_report_is_branch_isolated(self, branch, owner):
        from apps.accounts.tests.factories import BranchFactory, OwnerFactory

        other_branch = BranchFactory(code="VS56")
        other_owner = OwnerFactory(branch=other_branch)
        other_cashier = EmployeeFactory(branch=other_branch)
        v = stocked_variant(
            other_branch, other_owner, price="1000.00", quantity=5, unit_cost="600.00"
        )
        _sell(other_branch, other_cashier, other_owner, v, 3, "1000.00")

        report = profit_report(
            branch=branch, period="custom", start="2026-01-01", end="2026-12-31"
        )
        assert report["revenue"] == Decimal("0.00")
        assert report["net_profit"] == Decimal("0.00")


class TestBestAndSlow:
    def test_best_sellers_ranks_by_quantity(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        hot = stocked_variant(
            branch, owner, price="100.00", quantity=100, unit_cost="50"
        )
        warm = stocked_variant(
            branch, owner, price="100.00", quantity=100, unit_cost="50"
        )
        with freeze_time("2026-08-10 09:00:00"):
            _sell(branch, cashier, owner, hot, 20, "100.00")
            _sell(branch, cashier, owner, warm, 5, "100.00")
        rows = best_sellers(
            branch=branch,
            period="custom",
            start="2026-08-01",
            end="2026-08-31",
            limit=5,
        )
        assert [r["sku"] for r in rows][:2] == [hot.sku, warm.sku]
        assert rows[0]["quantity_sold"] == 20
        assert "unit_cost" not in rows[0] and "cogs" not in rows[0]

    def test_slow_movers_lists_stocked_but_unsold(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sold = stocked_variant(
            branch, owner, price="100.00", quantity=50, unit_cost="50"
        )
        idle = stocked_variant(
            branch, owner, price="100.00", quantity=50, unit_cost="50"
        )
        with freeze_time("2026-08-10 09:00:00"):
            _sell(branch, cashier, owner, sold, 10, "100.00")
        rows = slow_movers(
            branch=branch,
            period="custom",
            start="2026-08-01",
            end="2026-08-31",
            limit=10,
        )
        skus = {r["sku"] for r in rows}
        assert idle.sku in skus
        assert sold.sku not in skus


REPORTS = "/api/v1/reports"


class TestReportsApi:
    def test_employee_cannot_read_reports(self, login_as, employee):
        assert login_as(employee).get(f"{REPORTS}/profit/").status_code == 403

    def test_owner_profit_report_endpoint(self, login_as, owner, branch):
        cashier = EmployeeFactory(branch=branch)
        v = stocked_variant(
            branch, owner, price="1000.00", quantity=10, unit_cost="600.00"
        )
        with freeze_time("2026-08-10 09:00:00"):
            _sell(branch, cashier, owner, v, 2, "1000.00")
        res = login_as(owner).get(
            f"{REPORTS}/profit/?period=custom&start=2026-08-01&end=2026-08-31"
        )
        assert res.status_code == 200
        body = res.json()
        assert body["revenue"] == "2000.00"
        assert body["cogs"] == "1200.00"
        assert body["gross_profit"] == "800.00"

    def test_bad_custom_range_is_400(self, login_as, owner):
        res = login_as(owner).get(
            f"{REPORTS}/profit/?period=custom&start=2026-08-31&end=2026-08-01"
        )
        assert res.status_code == 400
