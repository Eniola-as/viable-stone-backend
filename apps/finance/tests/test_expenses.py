import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.core.exceptions import APIError, Conflict
from apps.finance.models import Expense, ExpenseCategory
from apps.finance.services.expenses import void_expense

from .factories import ExpenseCategoryFactory, ExpenseFactory

pytestmark = pytest.mark.django_db


class TestExpenseCategory:
    def test_name_is_case_insensitively_unique_per_branch(self, branch):
        ExpenseCategoryFactory(branch=branch, name="Transport")
        with pytest.raises(IntegrityError), transaction.atomic():
            ExpenseCategory.objects.create(branch=branch, name="transport")

    def test_same_name_allowed_in_other_branch(self):
        from apps.accounts.tests.factories import BranchFactory

        ExpenseCategoryFactory(branch=BranchFactory(), name="Rent")
        ExpenseCategoryFactory(branch=BranchFactory(), name="Rent")


class TestExpenseModel:
    def test_amount_must_be_positive(self, branch, owner):
        category = ExpenseCategoryFactory(branch=branch)
        with pytest.raises(IntegrityError), transaction.atomic():
            Expense.objects.create(
                branch=branch,
                category=category,
                amount=Decimal("0.00"),
                expense_date=datetime.date(2026, 8, 1),
                description="bad",
                created_by=owner,
            )

    def test_defaults(self, branch, owner):
        expense = ExpenseFactory(branch=branch)
        assert expense.is_voided is False
        assert expense.voided_by_id is None
        assert expense.voided_at is None


class TestVoidExpense:
    def test_void_preserves_amount_and_records_actor(self, branch, owner):
        expense = ExpenseFactory(branch=branch, amount=Decimal("5000.00"))
        void_expense(
            expense=expense, actor=owner, reason="Duplicate entry", request=None
        )
        expense.refresh_from_db()
        assert expense.is_voided is True
        assert expense.amount == Decimal("5000.00")  # never overwritten
        assert expense.void_reason == "Duplicate entry"
        assert expense.voided_by_id == owner.id
        assert expense.voided_at is not None

    def test_cannot_void_twice(self, branch, owner):
        expense = ExpenseFactory(branch=branch)
        void_expense(expense=expense, actor=owner, reason="x")
        with pytest.raises(Conflict):
            void_expense(expense=expense, actor=owner, reason="again")

    def test_void_requires_a_reason(self, branch, owner):
        expense = ExpenseFactory(branch=branch)
        with pytest.raises(APIError):
            void_expense(expense=expense, actor=owner, reason="   ")

    def test_void_writes_audit_row(self, branch, owner):
        from apps.core.models import AuditLog

        expense = ExpenseFactory(branch=branch)
        void_expense(expense=expense, actor=owner, reason="wrong category")
        assert AuditLog.objects.filter(
            action="expense.void", target_id=expense.id
        ).exists()


EXPENSES = "/api/v1/expenses/"
CATEGORIES = "/api/v1/expense-categories/"


class TestExpenseApi:
    def test_employee_cannot_touch_expenses(self, login_as, employee):
        assert login_as(employee).get(EXPENSES).status_code == 403
        assert login_as(employee).get(CATEGORIES).status_code == 403

    def test_owner_creates_category_then_expense(self, login_as, owner, branch):
        client = login_as(owner)
        cat = client.post(CATEGORIES, {"name": "Electricity"})
        assert cat.status_code == 201
        res = client.post(
            EXPENSES,
            {
                "category": cat.json()["id"],
                "amount": "12500.00",
                "expense_date": "2026-08-10",
                "description": "August PHCN bill",
            },
        )
        assert res.status_code == 201, res.content
        created = Expense.objects.get()
        assert created.branch_id == branch.id
        assert created.created_by_id == owner.id

    def test_void_endpoint(self, login_as, owner, branch):
        expense = ExpenseFactory(branch=branch, amount=Decimal("900.00"))
        res = login_as(owner).post(
            f"{EXPENSES}{expense.id}/void/", {"reason": "entered twice"}
        )
        assert res.status_code == 200
        expense.refresh_from_db()
        assert expense.is_voided is True

    def test_void_without_reason_rejected(self, login_as, owner, branch):
        expense = ExpenseFactory(branch=branch)
        res = login_as(owner).post(f"{EXPENSES}{expense.id}/void/", {"reason": ""})
        assert res.status_code == 400

    def test_expense_delete_is_blocked(self, login_as, owner, branch):
        expense = ExpenseFactory(branch=branch)
        assert login_as(owner).delete(f"{EXPENSES}{expense.id}/").status_code == 405

    def test_cross_branch_expense_is_404(self, login_as, owner):
        from apps.accounts.tests.factories import BranchFactory

        other = ExpenseFactory(branch=BranchFactory(code="VS55"))
        assert login_as(owner).get(f"{EXPENSES}{other.id}/").status_code == 404
