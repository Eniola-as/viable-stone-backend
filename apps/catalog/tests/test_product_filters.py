"""Server-side product filtering: `category`, `brand`, `kind`, `is_active`.

Exact model relationships, AND-combined, composable with `search` / `ordering`
/ pagination, branch-isolated, and honouring the employee active-only rule.
Invalid values return the standard validation envelope, never a 500.
"""

from __future__ import annotations

import uuid

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory
from apps.catalog.models import ProductKind
from apps.catalog.tests.factories import (
    BrandFactory,
    CategoryFactory,
    ProductFactory,
    ProductVariantFactory,
)

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/products/"

_COST_FIELDS = {
    "average_unit_cost",
    "unit_cost",
    "unit_cost_snapshot",
    "cost",
    "weighted_average_cost",
}


def _names(response) -> set[str]:
    body = response.json()
    return {row["name"] for row in body["results"]}


@pytest.fixture
def catalogue(db, branch):
    """A small branch catalogue with known category / brand / kind / active mix."""

    interior = CategoryFactory(branch=branch, name="Interior")
    tools = CategoryFactory(branch=branch, name="Tools")
    dulux = BrandFactory(branch=branch, name="Dulux")
    protool = BrandFactory(branch=branch, name="ProTool")

    made = {}
    made["paint_dulux_interior"] = ProductFactory(
        branch=branch,
        category=interior,
        brand=dulux,
        kind=ProductKind.PAINT,
        name="Vinyl Matt",
        is_active=True,
    )
    made["paint_dulux_interior_2"] = ProductFactory(
        branch=branch,
        category=interior,
        brand=dulux,
        kind=ProductKind.PAINT,
        name="Weathershield",
        is_active=True,
    )
    made["paint_protool_interior"] = ProductFactory(
        branch=branch,
        category=interior,
        brand=protool,
        kind=ProductKind.PAINT,
        name="Emulsion",
        is_active=True,
    )
    made["equip_protool_tools"] = ProductFactory(
        branch=branch,
        category=tools,
        brand=protool,
        kind=ProductKind.EQUIPMENT,
        name="Roller Set",
        is_active=True,
    )
    made["equip_dulux_tools_inactive"] = ProductFactory(
        branch=branch,
        category=tools,
        brand=dulux,
        kind=ProductKind.EQUIPMENT,
        name="Old Tray",
        is_active=False,
    )
    return {
        "interior": interior,
        "tools": tools,
        "dulux": dulux,
        "protool": protool,
        "products": made,
    }


# --------------------------------------------------------------------------- #
# each filter on its own                                                      #
# --------------------------------------------------------------------------- #


class TestSingleFilters:
    def test_filter_by_category(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"category": str(catalogue["tools"].id)})
        assert res.status_code == 200
        assert _names(res) == {"Roller Set", "Old Tray"}

    def test_filter_by_brand(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"brand": str(catalogue["protool"].id)})
        assert res.status_code == 200
        assert _names(res) == {"Emulsion", "Roller Set"}

    def test_filter_by_kind_paint(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"kind": "PAINT"})
        assert res.status_code == 200
        assert _names(res) == {"Vinyl Matt", "Weathershield", "Emulsion"}

    def test_filter_by_kind_equipment(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"kind": "EQUIPMENT"})
        assert _names(res) == {"Roller Set", "Old Tray"}

    def test_filter_by_is_active_true(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"is_active": "true"})
        assert "Old Tray" not in _names(res)
        assert res.json()["count"] == 4

    def test_filter_by_is_active_false(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"is_active": "false"})
        assert _names(res) == {"Old Tray"}


# --------------------------------------------------------------------------- #
# combined filters (AND), plus search / ordering / pagination                 #
# --------------------------------------------------------------------------- #


class TestCombinedFilters:
    def test_category_and_brand_and_kind_combine_with_and(
        self, login_as, owner, catalogue
    ):
        res = login_as(owner).get(
            PRODUCTS,
            {
                "category": str(catalogue["interior"].id),
                "brand": str(catalogue["dulux"].id),
                "kind": "PAINT",
            },
        )
        assert _names(res) == {"Vinyl Matt", "Weathershield"}

    def test_impossible_combination_is_empty_not_error(
        self, login_as, owner, catalogue
    ):
        res = login_as(owner).get(
            PRODUCTS,
            {"category": str(catalogue["tools"].id), "kind": "PAINT"},
        )
        assert res.status_code == 200
        assert res.json()["count"] == 0
        assert res.json()["results"] == []

    def test_filter_plus_search(self, login_as, owner, branch, catalogue):
        res = login_as(owner).get(
            PRODUCTS, {"brand": str(catalogue["dulux"].id), "search": "weather"}
        )
        assert _names(res) == {"Weathershield"}

    def test_filter_plus_ordering_and_pagination(self, login_as, owner, catalogue):
        page1 = (
            login_as(owner)
            .get(
                PRODUCTS,
                {"kind": "PAINT", "ordering": "name", "page": 1, "page_size": 2},
            )
            .json()
        )
        assert page1["count"] == 3
        assert [r["name"] for r in page1["results"]] == ["Emulsion", "Vinyl Matt"]
        page2 = (
            login_as(owner)
            .get(
                PRODUCTS,
                {"kind": "PAINT", "ordering": "name", "page": 2, "page_size": 2},
            )
            .json()
        )
        assert [r["name"] for r in page2["results"]] == ["Weathershield"]

    def test_no_owner_only_fields_leak_when_filtering(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"kind": "PAINT"})
        blob = res.content.decode()
        for row in res.json()["results"]:
            assert _COST_FIELDS.isdisjoint(row.keys())
            assert {"id", "category", "brand", "kind", "variants", "image_url"} <= set(
                row
            )
        assert "average_unit_cost" not in blob


# --------------------------------------------------------------------------- #
# invalid values -> standard safe envelope, never silently ignored / 500      #
# --------------------------------------------------------------------------- #


class TestInvalidFilterValues:
    def _assert_envelope(self, res, field):
        assert res.status_code == 400
        body = res.json()
        assert set(body) >= {"code", "message", "field_errors", "request_id"}
        assert body["code"] == "validation_error"
        assert field in body["field_errors"]

    def test_invalid_category_uuid(self, login_as, owner, catalogue):
        self._assert_envelope(
            login_as(owner).get(PRODUCTS, {"category": "not-a-uuid"}), "category"
        )

    def test_invalid_brand_uuid(self, login_as, owner, catalogue):
        self._assert_envelope(
            login_as(owner).get(PRODUCTS, {"brand": "12345"}), "brand"
        )

    def test_invalid_kind_value(self, login_as, owner, catalogue):
        self._assert_envelope(login_as(owner).get(PRODUCTS, {"kind": "paint"}), "kind")

    def test_invalid_is_active_value(self, login_as, owner, catalogue):
        self._assert_envelope(
            login_as(owner).get(PRODUCTS, {"is_active": "maybe"}), "is_active"
        )

    def test_multiple_invalid_values_are_all_reported(self, login_as, owner, catalogue):
        res = login_as(owner).get(
            PRODUCTS, {"category": "bad", "kind": "nope", "is_active": "x"}
        )
        assert res.status_code == 400
        assert set(res.json()["field_errors"]) == {"category", "kind", "is_active"}

    def test_unknown_category_uuid_is_empty_not_error(self, login_as, owner, catalogue):
        res = login_as(owner).get(PRODUCTS, {"category": str(uuid.uuid4())})
        assert res.status_code == 200
        assert res.json()["count"] == 0


# --------------------------------------------------------------------------- #
# branch isolation                                                           #
# --------------------------------------------------------------------------- #


class TestBranchIsolation:
    def test_other_branch_category_id_yields_nothing(self, login_as, owner, catalogue):
        other = BranchFactory(code="VS81")
        foreign_cat = CategoryFactory(branch=other, name="Interior")
        ProductFactory(branch=other, category=foreign_cat, name="Not Mine")

        res = login_as(owner).get(PRODUCTS, {"category": str(foreign_cat.id)})
        assert res.status_code == 200
        assert res.json()["count"] == 0
        assert "Not Mine" not in _names(res)

    def test_other_branch_brand_id_yields_nothing(self, login_as, owner, catalogue):
        other = BranchFactory(code="VS82")
        foreign_brand = BrandFactory(branch=other, name="Dulux")
        ProductFactory(branch=other, brand=foreign_brand, name="Foreign Paint")

        res = login_as(owner).get(PRODUCTS, {"brand": str(foreign_brand.id)})
        assert res.json()["count"] == 0


# --------------------------------------------------------------------------- #
# employee active-only visibility                                            #
# --------------------------------------------------------------------------- #


class TestEmployeeVisibility:
    def test_employee_is_active_false_reveals_nothing(
        self, login_as, branch, catalogue
    ):
        emp = EmployeeFactory(branch=branch)
        res = login_as(emp).get(PRODUCTS, {"is_active": "false"})
        assert res.status_code == 200
        assert res.json()["count"] == 0
        assert "Old Tray" not in _names(res)

    def test_employee_category_filter_still_hides_inactive(
        self, login_as, branch, catalogue
    ):
        emp = EmployeeFactory(branch=branch)
        res = login_as(emp).get(PRODUCTS, {"category": str(catalogue["tools"].id)})
        assert _names(res) == {"Roller Set"}  # "Old Tray" (inactive) excluded

    def test_employee_is_active_true_matches_unfiltered_active_set(
        self, login_as, branch, catalogue
    ):
        emp = EmployeeFactory(branch=branch)
        with_flag = login_as(emp).get(PRODUCTS, {"is_active": "true"}).json()["count"]
        without = login_as(emp).get(PRODUCTS).json()["count"]
        assert with_flag == without == 4

    def test_employee_variant_rows_never_leak_cost(self, login_as, branch, catalogue):
        emp = EmployeeFactory(branch=branch)
        ProductVariantFactory(
            product=catalogue["products"]["paint_dulux_interior"], sku="FLT-1"
        )
        res = login_as(emp).get(PRODUCTS, {"kind": "PAINT"})
        for row in res.json()["results"]:
            for variant in row["variants"]:
                assert _COST_FIELDS.isdisjoint(variant.keys())
