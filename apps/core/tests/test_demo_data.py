"""Stage 17 — the development demo-data command is safe and idempotent."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db

_ENV = {
    "DEMO_OWNER_PASSWORD": "demo-owner-pw-123",
    "DEMO_EMPLOYEE_PASSWORD": "demo-cashier-pw-123",
}


def test_refuses_to_run_under_production_settings(monkeypatch, settings):
    settings.DEBUG = False
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(CommandError, match="refuses to run"):
        call_command("seed_demo")


def test_requires_demo_passwords_from_the_environment(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.delenv("DEMO_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_EMPLOYEE_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="DEMO_OWNER_PASSWORD"):
        call_command("seed_demo")


def test_seeds_a_coherent_demo_slice_and_is_idempotent(monkeypatch, settings):
    settings.DEBUG = True
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)

    from apps.accounts.models import Branch, User
    from apps.finance.models import Expense
    from apps.inventory.models import InventoryBalance
    from apps.sales.models import Sale

    call_command("seed_demo")
    call_command("seed_demo")  # second run must not duplicate

    branch = Branch.objects.get(code="DEMO")
    assert User.objects.filter(branch=branch, role="OWNER").count() == 1
    assert User.objects.filter(branch=branch, role="EMPLOYEE").count() == 1
    assert Sale.objects.filter(branch=branch, status="COMPLETED").count() == 1
    assert Expense.objects.filter(branch=branch).count() == 1
    balance = InventoryBalance.objects.get(branch=branch)
    assert balance.quantity == 38  # 40 opening minus the 2-unit demo sale

    owner = User.objects.get(username="demo-owner")
    assert owner.check_password("demo-owner-pw-123")
    assert owner.mfa_required is True
