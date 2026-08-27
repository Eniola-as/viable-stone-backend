import datetime

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import BranchFactory, OwnerFactory
from apps.finance.models import Expense, ExpenseCategory


class ExpenseCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ExpenseCategory

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Category {n}")


class ExpenseFactory(DjangoModelFactory):
    class Meta:
        model = Expense

    branch = factory.SubFactory(BranchFactory)
    category = factory.SubFactory(
        ExpenseCategoryFactory, branch=factory.SelfAttribute("..branch")
    )
    created_by = factory.SubFactory(
        OwnerFactory, branch=factory.SelfAttribute("..branch")
    )
    amount = factory.Faker(
        "pydecimal", left_digits=5, right_digits=2, positive=True, min_value=100
    )
    expense_date = factory.LazyFunction(datetime.date.today)
    description = "Transport for stock pickup"
