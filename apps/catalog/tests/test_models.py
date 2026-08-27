import pytest
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import BranchFactory
from apps.catalog.models import Brand, Category, PriceHistory

from .factories import (
    BrandFactory,
    CategoryFactory,
    ProductFactory,
    ProductVariantFactory,
)

pytestmark = pytest.mark.django_db


class TestCategoryBrandUniqueness:
    def test_category_name_is_case_insensitively_unique_per_branch(self):
        branch = BranchFactory()
        CategoryFactory(branch=branch, name="Paint")
        with pytest.raises(IntegrityError), transaction.atomic():
            Category.objects.create(branch=branch, name="paint")

    def test_same_category_name_allowed_in_another_branch(self):
        CategoryFactory(branch=BranchFactory(), name="Paint")
        CategoryFactory(branch=BranchFactory(), name="Paint")

    def test_brand_name_is_case_insensitively_unique_per_branch(self):
        branch = BranchFactory()
        BrandFactory(branch=branch, name="Dulux")
        with pytest.raises(IntegrityError), transaction.atomic():
            Brand.objects.create(branch=branch, name="DULUX")


class TestProductVariant:
    def test_sku_is_normalised_to_uppercase(self):
        variant = ProductVariantFactory(sku="  dul-emu-wht-20l ")
        assert variant.sku == "DUL-EMU-WHT-20L"

    def test_sku_is_globally_unique(self):
        ProductVariantFactory(sku="DUP-1")
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductVariantFactory(sku="dup-1")

    def test_same_attribute_combo_rejected_for_one_product(self):
        product = ProductFactory()
        ProductVariantFactory(
            product=product, colour="White", size="20 litres", finish="Matte"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductVariantFactory(
                product=product, colour="white", size="20 LITRES", finish="matte"
            )

    def test_blank_barcode_does_not_collide(self):
        ProductVariantFactory(barcode=None)
        ProductVariantFactory(barcode=None)  # no IntegrityError

    def test_barcode_is_case_insensitively_unique_when_present(self):
        ProductVariantFactory(barcode="ABC123")
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductVariantFactory(barcode="abc123")

    def test_description_property(self):
        variant = ProductVariantFactory(colour="Blue", size="5 litres", finish="")
        assert variant.description == "Blue / 5 litres"


class TestPriceHistoryConstraints:
    def test_amount_must_be_positive(self, owner):
        variant = ProductVariantFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceHistory.objects.create(
                variant=variant,
                amount=0,
                valid_from="2026-01-01T00:00:00Z",
                changed_by=owner,
            )

    def test_only_one_open_price_row_per_variant(self, owner):
        variant = ProductVariantFactory()
        PriceHistory.objects.create(
            variant=variant,
            amount="1000.00",
            valid_from="2026-01-01T00:00:00Z",
            changed_by=owner,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceHistory.objects.create(
                variant=variant,
                amount="1200.00",
                valid_from="2026-02-01T00:00:00Z",
                changed_by=owner,
            )
