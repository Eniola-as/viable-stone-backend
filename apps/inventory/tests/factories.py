import datetime

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import BranchFactory, OwnerFactory
from apps.inventory.models import Restock, Supplier


class SupplierFactory(DjangoModelFactory):
    class Meta:
        model = Supplier

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Supplier {n}")
    phone = "08030000000"


class RestockFactory(DjangoModelFactory):
    class Meta:
        model = Restock

    branch = factory.SubFactory(BranchFactory)
    supplier = factory.SubFactory(
        SupplierFactory, branch=factory.SelfAttribute("..branch")
    )
    created_by = factory.SubFactory(
        OwnerFactory, branch=factory.SelfAttribute("..branch")
    )
    date = factory.LazyFunction(datetime.date.today)
