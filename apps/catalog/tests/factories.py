import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import BranchFactory
from apps.catalog.models import Brand, Category, Product, ProductKind, ProductVariant


class CategoryFactory(DjangoModelFactory):
    class Meta:
        model = Category

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Category {n}")


class BrandFactory(DjangoModelFactory):
    class Meta:
        model = Brand

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Brand {n}")


class ProductFactory(DjangoModelFactory):
    class Meta:
        model = Product

    branch = factory.SubFactory(BranchFactory)
    category = factory.SubFactory(
        CategoryFactory, branch=factory.SelfAttribute("..branch")
    )
    brand = factory.SubFactory(BrandFactory, branch=factory.SelfAttribute("..branch"))
    kind = ProductKind.PAINT
    name = factory.Sequence(lambda n: f"Product {n}")


class ProductVariantFactory(DjangoModelFactory):
    class Meta:
        model = ProductVariant

    product = factory.SubFactory(ProductFactory)
    colour = "White"
    size = factory.Sequence(lambda n: f"{n + 1} litres")
    finish = "Matte"
    sku = factory.Sequence(lambda n: f"SKU-{n:05d}")
    low_stock_level = 5
