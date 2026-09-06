"""G18 / G13 — lock the best-sellers / slow-movers response shape + limit rules.

Runtime returns a **bare array** (no ``{count,results}`` envelope); ``limit``
(1..100, default 10 best / 20 slow) is the only row control — ``page`` /
``page_size`` do nothing on these two report actions. The OpenAPI schema must
say so.
"""

import pytest
from freezegun import freeze_time

from apps.accounts.tests.factories import EmployeeFactory
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

REPORTS = "/api/v1/reports"


def _sell(branch, cashier, owner, variant, qty, price):
    create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=qty)],
        payments=[PaymentLine(method="CASH", amount=price)],
        client_sale_id=__import__("uuid").uuid4(),
    )


class TestBareArrayResponse:
    def test_best_sellers_returns_a_bare_array(self, login_as, owner, branch):
        cashier = EmployeeFactory(branch=branch)
        v = stocked_variant(
            branch, owner, price="1000.00", quantity=10, unit_cost="600"
        )
        with freeze_time("2026-08-10 09:00:00"):
            _sell(branch, cashier, owner, v, 2, "2000.00")
        res = login_as(owner).get(f"{REPORTS}/best-sellers/?period=month")
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body, list)  # NOT {"results": [...]}

    def test_slow_movers_returns_a_bare_array(self, login_as, owner, branch):
        stocked_variant(branch, owner, price="500.00", quantity=5, unit_cost="200")
        res = login_as(owner).get(f"{REPORTS}/slow-movers/?period=month")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_limit_caps_rows_and_page_params_are_ignored(self, login_as, owner, branch):
        cashier = EmployeeFactory(branch=branch)
        # Freeze the whole test — sales AND the `period=month` query — to one
        # instant so the sales always fall inside the resolved window,
        # regardless of the real calendar date.
        with freeze_time("2026-08-10 09:00:00"):
            for i in range(4):
                v = stocked_variant(
                    branch,
                    owner,
                    price="100.00",
                    quantity=10,
                    unit_cost="60",
                    sku=f"BS-{i}",
                )
                _sell(branch, cashier, owner, v, i + 1, str((i + 1) * 100))
            res = login_as(owner).get(
                f"{REPORTS}/best-sellers/?period=month&limit=2&page=2&page_size=1"
            )
        assert res.status_code == 200
        rows = res.json()
        assert isinstance(rows, list)
        assert len(rows) == 2  # limit honoured; page/page_size ignored


class TestOpenAPIShape:
    def test_schema_declares_bare_arrays_not_paginated(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        for path in ("/api/v1/reports/best-sellers/", "/api/v1/reports/slow-movers/"):
            resp = schema["paths"][path]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
            assert resp.get("type") == "array", (path, resp)
            assert "Paginated" not in str(resp)
