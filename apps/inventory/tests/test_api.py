from decimal import Decimal

import pytest

from apps.accounts.tests.factories import EmployeeFactory
from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.inventory.models import InventoryBalance, Restock, RestockStatus
from apps.inventory.services.stock import open_stock

from .factories import SupplierFactory

pytestmark = pytest.mark.django_db

SUPPLIERS = "/api/v1/suppliers/"
RESTOCKS = "/api/v1/restocks/"
INVENTORY = "/api/v1/inventory/"


class TestSupplierIsOwnerOnly:
    def test_employee_gets_403_on_suppliers(self, login_as, employee):
        assert login_as(employee).get(SUPPLIERS).status_code == 403

    def test_owner_can_create_supplier(self, login_as, owner, branch):
        res = login_as(owner).post(SUPPLIERS, {"name": "PaintCo", "phone": "0803"})
        assert res.status_code == 201
        assert res.json()["name"] == "PaintCo"

    def test_supplier_delete_blocked(self, login_as, owner, branch):
        supplier = SupplierFactory(branch=branch)
        assert login_as(owner).delete(f"{SUPPLIERS}{supplier.id}/").status_code == 405


class TestRestockFlow:
    def test_create_draft_then_confirm(self, login_as, owner, branch):
        supplier = SupplierFactory(branch=branch)
        product = ProductFactory(branch=branch)
        v1 = ProductVariantFactory(product=product)
        v2 = ProductVariantFactory(product=product)
        client = login_as(owner)

        create = client.post(
            RESTOCKS,
            {
                "supplier": str(supplier.id),
                "date": "2026-08-01",
                "items": [
                    {"variant": str(v1.id), "quantity": 10, "unit_cost": "100.00"},
                    {"variant": str(v2.id), "quantity": 4, "unit_cost": "250.00"},
                ],
            },
            format="json",
        )
        assert create.status_code == 201, create.content
        restock_id = create.json()["id"]

        confirm = client.post(f"{RESTOCKS}{restock_id}/confirm/")
        assert confirm.status_code == 200
        assert confirm.json()["status"] == "CONFIRMED"
        assert confirm.json()["total_cost"] == "2000.00"

        assert InventoryBalance.objects.get(branch=branch, variant=v1).quantity == 10
        assert InventoryBalance.objects.get(branch=branch, variant=v2).quantity == 4

        # Second confirm is a conflict.
        again = client.post(f"{RESTOCKS}{restock_id}/confirm/")
        assert again.status_code == 409

    def test_confirmed_restock_cannot_be_edited(self, login_as, owner, branch):
        supplier = SupplierFactory(branch=branch)
        variant = ProductVariantFactory(product=ProductFactory(branch=branch))
        client = login_as(owner)
        rid = client.post(
            RESTOCKS,
            {
                "supplier": str(supplier.id),
                "date": "2026-08-01",
                "items": [
                    {"variant": str(variant.id), "quantity": 2, "unit_cost": "10.00"}
                ],
            },
            format="json",
        ).json()["id"]
        client.post(f"{RESTOCKS}{rid}/confirm/")
        res = client.patch(f"{RESTOCKS}{rid}/", {"supplier_invoice_number": "X"})
        assert res.status_code == 409

    def test_duplicate_variant_in_items_rejected(self, login_as, owner, branch):
        supplier = SupplierFactory(branch=branch)
        variant = ProductVariantFactory(product=ProductFactory(branch=branch))
        res = login_as(owner).post(
            RESTOCKS,
            {
                "supplier": str(supplier.id),
                "date": "2026-08-01",
                "items": [
                    {"variant": str(variant.id), "quantity": 1, "unit_cost": "5.00"},
                    {"variant": str(variant.id), "quantity": 2, "unit_cost": "6.00"},
                ],
            },
            format="json",
        )
        assert res.status_code == 400


class TestInventoryVisibility:
    def test_employee_inventory_hides_cost(self, login_as, owner, branch):
        emp = EmployeeFactory(branch=branch)
        variant = ProductVariantFactory(product__branch=branch, product__is_active=True)
        open_stock(
            branch=branch,
            variant=variant,
            quantity=12,
            unit_cost=Decimal("900.00"),
            created_by=owner,
        )
        row = login_as(emp).get(INVENTORY).json()["results"][0]
        assert row["quantity"] == 12
        assert "average_unit_cost" not in row
        assert "stock_value" not in row

    def test_owner_inventory_shows_cost(self, login_as, owner, branch):
        variant = ProductVariantFactory(product__branch=branch)
        open_stock(
            branch=branch,
            variant=variant,
            quantity=12,
            unit_cost=Decimal("900.00"),
            created_by=owner,
        )
        row = login_as(owner).get(INVENTORY).json()["results"][0]
        assert row["average_unit_cost"] == "900.00"
        assert row["stock_value"] == "10800.00"

    def test_stock_value_is_owner_only(self, login_as, owner, branch):
        emp = EmployeeFactory(branch=branch)
        assert login_as(emp).get(f"{INVENTORY}stock-value/").status_code == 403
        assert login_as(owner).get(f"{INVENTORY}stock-value/").status_code == 200

    def test_low_stock_endpoint(self, login_as, owner, branch):
        low = ProductVariantFactory(product__branch=branch, low_stock_level=10)
        ok = ProductVariantFactory(product__branch=branch, low_stock_level=1)
        open_stock(
            branch=branch,
            variant=low,
            quantity=3,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        open_stock(
            branch=branch,
            variant=ok,
            quantity=50,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        body = login_as(owner).get(f"{INVENTORY}low-stock/").json()
        skus = {r["variant_sku"] for r in body["results"]}
        assert low.sku in skus and ok.sku not in skus

    def test_movements_endpoint_owner_only(self, login_as, owner, branch):
        emp = EmployeeFactory(branch=branch)
        variant = ProductVariantFactory(product__branch=branch)
        open_stock(
            branch=branch,
            variant=variant,
            quantity=5,
            unit_cost=Decimal("1"),
            created_by=owner,
        )
        assert (
            login_as(emp).get(f"{INVENTORY}movements/?variant={variant.id}").status_code
            == 403
        )
        res = login_as(owner).get(f"{INVENTORY}movements/?variant={variant.id}")
        assert res.status_code == 200
        assert res.json()["results"][0]["movement_type"] == "OPENING"

    def test_opening_endpoint_rejects_second_call(self, login_as, owner, branch):
        variant = ProductVariantFactory(product__branch=branch)
        client = login_as(owner)
        payload = {"variant": str(variant.id), "quantity": 5, "unit_cost": "10.00"}
        assert client.post(f"{INVENTORY}opening/", payload).status_code == 201
        assert client.post(f"{INVENTORY}opening/", payload).status_code == 409


def test_restock_is_branch_scoped(login_as, owner):
    from apps.accounts.tests.factories import BranchFactory

    from .factories import RestockFactory

    other = RestockFactory(branch=BranchFactory(code="VS80"))
    res = login_as(owner).get(f"{RESTOCKS}{other.id}/")
    assert res.status_code == 404
    assert Restock.objects.filter(status=RestockStatus.DRAFT).exists()
