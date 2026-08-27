"""Development-only demo data.

Creates a small, coherent slice of the business — one branch, an owner and an
employee, a few categories / products with opening stock, a couple of sales
with split payments, and an expense — so the frontend and manual testers have
something to work against.

Guarantees:
* Refuses to run under production settings.
* Idempotent: re-running updates in place, ``--reset`` wipes the demo branch
  first.
* No password or real customer / business data is committed here — demo
  credentials come from environment variables (``DEMO_OWNER_PASSWORD``,
  ``DEMO_EMPLOYEE_PASSWORD``) or, with ``--interactive``, a secure prompt.
"""

from __future__ import annotations

import os
from decimal import Decimal
from getpass import getpass

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEMO_BRANCH_CODE = "DEMO"


class Command(BaseCommand):
    help = "Populate development demo data (never runs under production settings)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the existing demo branch and its data first.",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Prompt for demo passwords instead of reading the environment.",
        )

    def handle(self, *args, **options):
        self._guard_environment()
        owner_pw, employee_pw = self._credentials(options["interactive"])

        with transaction.atomic():
            if options["reset"]:
                self._wipe()
            branch = self._branch()
            owner = self._user(
                "demo-owner", "OWNER", branch, owner_pw, mfa_required=True
            )
            self._user("demo-cashier", "EMPLOYEE", branch, employee_pw)
            self._catalogue_stock_and_sales(branch, owner)

        self.stdout.write(
            self.style.SUCCESS(
                "Demo data ready. Branch code 'DEMO'. Sign in as demo-owner / "
                "demo-cashier with the passwords you supplied."
            )
        )

    # -- guards ----------------------------------------------------------- #

    def _guard_environment(self):
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if module.endswith(".production") or not settings.DEBUG:
            raise CommandError(
                "seed_demo refuses to run outside DEBUG / development settings."
            )

    def _credentials(self, interactive: bool):
        if interactive:
            owner_pw = getpass("Demo owner password: ")
            employee_pw = getpass("Demo cashier password: ")
        else:
            owner_pw = os.environ.get("DEMO_OWNER_PASSWORD", "")
            employee_pw = os.environ.get("DEMO_EMPLOYEE_PASSWORD", "")
        if not owner_pw or not employee_pw:
            raise CommandError(
                "Set DEMO_OWNER_PASSWORD and DEMO_EMPLOYEE_PASSWORD (or pass "
                "--interactive). They are never stored in the repository."
            )
        if len(owner_pw) < 10 or len(employee_pw) < 10:
            raise CommandError("Demo passwords must be at least 10 characters.")
        return owner_pw, employee_pw

    # -- builders ------------------------------------------------------- #

    def _wipe(self):
        from apps.accounts.models import Branch

        Branch.objects.filter(code=DEMO_BRANCH_CODE).delete()

    def _branch(self):
        from apps.accounts.models import Branch

        branch, _ = Branch.objects.update_or_create(
            code=DEMO_BRANCH_CODE,
            defaults={"name": "Demo Paint Shop", "receipt_prefix": "DEMO"},
        )
        return branch

    def _user(self, username, role, branch, password, *, mfa_required=False):
        from apps.accounts.models import User

        user, _ = User.objects.update_or_create(
            username=username,
            defaults={
                "role": role,
                "branch": branch,
                "is_active": True,
                "must_change_password": False,
                "mfa_required": mfa_required,
            },
        )
        user.set_password(password)
        user.save()
        return user

    def _catalogue_stock_and_sales(self, branch, owner):
        import uuid

        from apps.catalog.models import Category, Product, ProductKind, ProductVariant
        from apps.catalog.services.pricing import set_active_price
        from apps.finance.models import Expense, ExpenseCategory
        from apps.inventory.services.stock import lock_balances, open_stock
        from apps.sales.services.sales import CartLine, PaymentLine, create_sale

        category, _ = Category.objects.update_or_create(branch=branch, name="Emulsion")
        product, _ = Product.objects.update_or_create(
            branch=branch,
            name="Demo Wall Paint",
            defaults={"category": category, "kind": ProductKind.PAINT},
        )
        specs = [("DEMO-WHT-5L", "White", "5 litres", "8500.00", 40, "5200.00")]
        variants = []
        for sku, colour, size, price, qty, cost in specs:
            variant, _ = ProductVariant.objects.update_or_create(
                sku=sku,
                defaults={
                    "product": product,
                    "colour": colour,
                    "size": size,
                    "finish": "Matte",
                    "low_stock_level": 8,
                    "is_active": True,
                },
            )
            set_active_price(variant=variant, amount=Decimal(price), changed_by=owner)
            balance = lock_balances(branch, [variant.id])[variant.id]
            if balance.quantity == 0:
                open_stock(
                    branch=branch,
                    variant=variant,
                    quantity=qty,
                    unit_cost=Decimal(cost),
                    created_by=owner,
                )
            variants.append(variant)

        if not branch.sales.exists():
            create_sale(
                branch=branch,
                cashier=owner,
                cart=[CartLine(variant_id=variants[0].id, quantity=2)],
                payments=[
                    PaymentLine(
                        method="CASH",
                        amount=Decimal("10000.00"),
                        tendered_amount=Decimal("10000.00"),
                    ),
                    PaymentLine(method="POS", amount=Decimal("7000.00")),
                ],
                client_sale_id=uuid.uuid4(),
            )

        exp_category, _ = ExpenseCategory.objects.update_or_create(
            branch=branch, name="Utilities"
        )
        if not Expense.objects.filter(branch=branch).exists():
            Expense.objects.create(
                branch=branch,
                category=exp_category,
                amount=Decimal("3500.00"),
                expense_date="2026-08-01",
                description="Demo electricity bill",
                created_by=owner,
            )
