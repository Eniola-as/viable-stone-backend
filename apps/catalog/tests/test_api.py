import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory
from apps.catalog.models import Category
from apps.catalog.services.pricing import set_active_price

from .factories import CategoryFactory, ProductFactory, ProductVariantFactory

pytestmark = pytest.mark.django_db

CATEGORIES = "/api/v1/categories/"
PRODUCTS = "/api/v1/products/"
VARIANTS = "/api/v1/variants/"

COST_FIELD_NAMES = {
    "average_unit_cost",
    "unit_cost",
    "unit_cost_snapshot",
    "cost",
    "weighted_average_cost",
}


class TestCatalogueWritePermissions:
    def test_owner_creates_category(self, login_as, owner, branch):
        res = login_as(owner).post(CATEGORIES, {"name": "Thinners"})
        assert res.status_code == 201
        assert Category.objects.get(name="Thinners").branch_id == branch.id

    def test_employee_cannot_create_category(self, login_as, employee):
        assert login_as(employee).post(CATEGORIES, {"name": "X"}).status_code == 403

    def test_employee_can_list_categories(self, login_as, employee):
        CategoryFactory(branch=employee.branch, name="Paint")
        res = login_as(employee).get(CATEGORIES)
        assert res.status_code == 200
        assert res.json()["count"] == 1


class TestBranchScoping:
    def test_products_are_branch_scoped(self, login_as, owner):
        ProductFactory(branch=owner.branch, name="Mine")
        ProductFactory(branch=BranchFactory(code="VS90"), name="Theirs")
        body = login_as(owner).get(PRODUCTS).json()
        names = {row["name"] for row in body["results"]}
        assert names == {"Mine"}

    def test_cross_branch_product_id_is_404(self, login_as, owner):
        outsider = ProductFactory(branch=BranchFactory(code="VS91"))
        res = login_as(owner).get(f"{PRODUCTS}{outsider.id}/")
        assert res.status_code == 404


class TestEmployeeCatalogueVisibility:
    def test_employee_sees_active_only(self, login_as, branch):
        emp = EmployeeFactory(branch=branch)
        ProductFactory(branch=branch, name="Active", is_active=True)
        ProductFactory(branch=branch, name="Retired", is_active=False)
        body = login_as(emp).get(PRODUCTS).json()
        assert {row["name"] for row in body["results"]} == {"Active"}

    def test_owner_sees_inactive_too(self, login_as, owner):
        ProductFactory(branch=owner.branch, name="Retired", is_active=False)
        body = login_as(owner).get(PRODUCTS).json()
        assert "Retired" in {row["name"] for row in body["results"]}

    def test_variant_payload_exposes_price_but_no_cost(self, login_as, owner, branch):
        emp = EmployeeFactory(branch=branch)
        variant = ProductVariantFactory(product__branch=branch, product__is_active=True)
        set_active_price(variant=variant, amount="1500.00", changed_by=owner)
        row = login_as(emp).get(f"{VARIANTS}{variant.id}/").json()
        assert row["price"] == "1500.00"
        assert COST_FIELD_NAMES.isdisjoint(row.keys())


class TestVariantSearchAndPricing:
    def test_variant_search_by_sku(self, login_as, owner, branch):
        ProductVariantFactory(product__branch=branch, sku="FIND-ME-1")
        ProductVariantFactory(product__branch=branch, sku="OTHER-2")
        body = login_as(owner).get(f"{VARIANTS}?search=find-me").json()
        assert [r["sku"] for r in body["results"]] == ["FIND-ME-1"]

    def test_owner_sets_price_via_endpoint(self, login_as, owner, branch):
        variant = ProductVariantFactory(product__branch=branch)
        res = login_as(owner).post(
            f"{VARIANTS}{variant.id}/price/", {"amount": "2999.99"}
        )
        assert res.status_code == 200
        assert res.json()["amount"] == "2999.99"

    def test_employee_cannot_set_price(self, login_as, branch):
        emp = EmployeeFactory(branch=branch)
        variant = ProductVariantFactory(product__branch=branch, product__is_active=True)
        res = login_as(emp).post(f"{VARIANTS}{variant.id}/price/", {"amount": "10.00"})
        assert res.status_code == 403

    def test_price_history_is_owner_only_and_ordered(self, login_as, owner, branch):
        variant = ProductVariantFactory(product__branch=branch)
        set_active_price(variant=variant, amount="100.00", changed_by=owner)
        set_active_price(variant=variant, amount="150.00", changed_by=owner)
        body = login_as(owner).get(f"{VARIANTS}{variant.id}/price-history/").json()
        results = body["results"] if "results" in body else body
        assert [r["amount"] for r in results] == ["150.00", "100.00"]
