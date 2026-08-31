"""G4 / G5 — the inventory summary/ledger endpoints must be honestly typed.

Runtime behaviour is already correct and unchanged; only the generated OpenAPI
schema was wrong (single ``InventoryBalanceEmployee`` object where the runtime
returns a paginated list, or a ``{stock_value}`` summary). These lock the real
shapes so the schema cannot drift back.
"""

import pytest

from apps.accounts.tests.factories import EmployeeFactory
from apps.catalog.tests.factories import ProductVariantFactory
from apps.inventory.services.stock import open_stock

pytestmark = pytest.mark.django_db

INV = "/api/v1/inventory"


def _stocked(branch, owner, *, qty, cost, low=100):
    v = ProductVariantFactory(product__branch=branch, low_stock_level=low)
    open_stock(branch=branch, variant=v, quantity=qty, unit_cost=cost, created_by=owner)
    return v


class TestRuntimeShapes:
    def test_low_stock_is_a_paginated_list(self, login_as, owner, branch):
        _stocked(branch, owner, qty=1, cost="10", low=100)  # below threshold
        body = login_as(owner).get(f"{INV}/low-stock/").json()
        assert set(body) >= {"count", "results"}
        assert isinstance(body["results"], list)

    def test_stock_value_is_a_summary_object(self, login_as, owner, branch):
        _stocked(branch, owner, qty=2, cost="50")
        body = login_as(owner).get(f"{INV}/stock-value/").json()
        assert set(body) == {"stock_value"}
        assert body["stock_value"] == "100.00"

    def test_movements_needs_variant_and_returns_a_paginated_ledger(
        self, login_as, owner, branch
    ):
        v = _stocked(branch, owner, qty=3, cost="20")
        missing = login_as(owner).get(f"{INV}/movements/")
        assert missing.status_code == 400
        assert missing.json()["code"] == "variant_required"

        body = login_as(owner).get(f"{INV}/movements/?variant={v.id}").json()
        assert set(body) >= {"count", "results"}
        assert body["results"][0]["movement_type"] == "OPENING"

    def test_movements_is_owner_only(self, login_as, owner, branch):
        v = _stocked(branch, owner, qty=1, cost="5")
        emp = EmployeeFactory(branch=branch)
        assert login_as(emp).get(f"{INV}/movements/?variant={v.id}").status_code == 403


@pytest.fixture
def schema():
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


class TestOpenAPIShapes:
    def test_low_stock_documents_a_paginated_list(self, schema):
        resp = schema["paths"][f"{INV}/low-stock/"]["get"]["responses"]["200"]
        ref = str(resp["content"]["application/json"]["schema"])
        assert "Paginated" in ref and "InventoryBalance" in ref

    def test_stock_value_documents_a_summary(self, schema):
        resp = schema["paths"][f"{INV}/stock-value/"]["get"]["responses"]["200"]
        ref = resp["content"]["application/json"]["schema"]
        assert "StockValue" in str(ref)
        assert "InventoryBalanceEmployee" not in str(ref)

    def test_movements_documents_variant_param_and_stockmovement_list(self, schema):
        op = schema["paths"][f"{INV}/movements/"]["get"]
        params = {p["name"]: p for p in op["parameters"]}
        assert "variant" in params and params["variant"]["required"] is True
        ref = str(op["responses"]["200"]["content"]["application/json"]["schema"])
        assert "StockMovement" in ref
        assert "InventoryBalanceEmployee" not in ref
