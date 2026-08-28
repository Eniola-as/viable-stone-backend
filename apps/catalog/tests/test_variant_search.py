"""Catalogue variant search (spec item 2).

``GET /api/v1/variants/?search=`` must match on the product name, SKU, barcode,
colour, size, finish, brand name and category name — while still respecting
branch isolation and the employee active-only visibility rule.
"""

from __future__ import annotations

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory
from apps.catalog.tests.factories import (
    BrandFactory,
    CategoryFactory,
    ProductFactory,
    ProductVariantFactory,
)

pytestmark = pytest.mark.django_db

VARIANTS = "/api/v1/variants/"


@pytest.fixture
def catalogue(db, branch):
    """One well-known variant plus a decoy, both in the caller's branch."""

    brand = BrandFactory(branch=branch, name="Dulux")
    category = CategoryFactory(branch=branch, name="Emulsion")
    product = ProductFactory(
        branch=branch, brand=brand, category=category, name="Weathershield"
    )
    target = ProductVariantFactory(
        product=product,
        colour="Red",
        size="5 litres",
        finish="Matte",
        sku="WS-RED-5L",
        barcode="6001234567890",
    )
    decoy_product = ProductFactory(branch=branch, name="Undercoat")
    ProductVariantFactory(
        product=decoy_product,
        colour="White",
        size="1 litre",
        finish="Gloss",
        sku="UC-WHT-1L",
        barcode="6009999999999",
    )
    return target


def _skus(response) -> list[str]:
    body = response.json()
    rows = body["results"] if isinstance(body, dict) else body
    return [r["sku"] for r in rows]


@pytest.mark.parametrize(
    "term",
    [
        "weathershield",  # product name
        "WS-RED-5L",  # sku
        "6001234567890",  # barcode
        "red",  # colour
        "5 litres",  # size
        "matte",  # finish
        "dulux",  # brand name
        "emulsion",  # category name
    ],
)
def test_variant_search_matches_every_supported_field(login_as, owner, catalogue, term):
    res = login_as(owner).get(VARIANTS, {"search": term})
    assert res.status_code == 200
    assert _skus(res) == [catalogue.sku], f"search={term!r}"


def test_variant_search_is_branch_isolated(login_as, owner, catalogue):
    """A perfectly matching variant in another branch is never returned."""

    other = BranchFactory(code="VS88")
    other_brand = BrandFactory(branch=other, name="Dulux")
    other_cat = CategoryFactory(branch=other, name="Emulsion")
    ProductVariantFactory(
        product=ProductFactory(
            branch=other, brand=other_brand, category=other_cat, name="Weathershield"
        ),
        colour="Red",
        size="5 litres",
        finish="Matte",
        sku="OTHER-RED-5L",
    )
    res = login_as(owner).get(VARIANTS, {"search": "weathershield red matte"})
    assert _skus(res) == [catalogue.sku]


def test_variant_search_respects_employee_active_only_visibility(login_as, branch):
    employee = EmployeeFactory(branch=branch)
    hidden = ProductVariantFactory(
        product=ProductFactory(branch=branch, name="Weathershield", is_active=True),
        colour="Red",
        size="5 litres",
        finish="Matte",
        sku="HIDDEN-1",
        is_active=False,
    )
    res = login_as(employee).get(VARIANTS, {"search": "weathershield"})
    assert hidden.sku not in _skus(res)


def test_variant_search_combined_terms_narrow_to_one_row(login_as, owner, catalogue):
    res = login_as(owner).get(VARIANTS, {"search": "red 5 litres matte"})
    assert _skus(res) == [catalogue.sku]
