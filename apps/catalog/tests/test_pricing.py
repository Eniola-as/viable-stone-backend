from decimal import Decimal

import pytest

from apps.catalog.models import PriceHistory
from apps.catalog.selectors import current_price
from apps.catalog.services.pricing import set_active_price
from apps.core.exceptions import APIError

from .factories import ProductVariantFactory

pytestmark = pytest.mark.django_db


def test_first_price_creates_single_open_row(owner):
    variant = ProductVariantFactory()
    row = set_active_price(variant=variant, amount=Decimal("2500.00"), changed_by=owner)
    assert row.valid_to is None
    assert current_price(variant).amount == Decimal("2500.00")
    assert PriceHistory.objects.filter(variant=variant).count() == 1


def test_new_price_closes_previous_row(owner):
    variant = ProductVariantFactory()
    first = set_active_price(
        variant=variant, amount=Decimal("2500.00"), changed_by=owner
    )
    second = set_active_price(
        variant=variant, amount=Decimal("3000.00"), changed_by=owner
    )
    first.refresh_from_db()
    assert first.valid_to is not None
    assert first.valid_to == second.valid_from
    assert current_price(variant).amount == Decimal("3000.00")
    assert (
        PriceHistory.objects.filter(variant=variant, valid_to__isnull=True).count() == 1
    )


def test_setting_same_price_is_noop(owner):
    variant = ProductVariantFactory()
    first = set_active_price(
        variant=variant, amount=Decimal("2500.00"), changed_by=owner
    )
    again = set_active_price(
        variant=variant, amount=Decimal("2500.00"), changed_by=owner
    )
    assert first.pk == again.pk
    assert PriceHistory.objects.filter(variant=variant).count() == 1


def test_zero_or_negative_price_rejected(owner):
    variant = ProductVariantFactory()
    with pytest.raises(APIError):
        set_active_price(variant=variant, amount=Decimal("0"), changed_by=owner)


def test_history_rows_are_never_rewritten(owner):
    variant = ProductVariantFactory()
    set_active_price(variant=variant, amount=Decimal("1000.00"), changed_by=owner)
    set_active_price(variant=variant, amount=Decimal("1100.00"), changed_by=owner)
    set_active_price(variant=variant, amount=Decimal("1250.00"), changed_by=owner)
    amounts = list(
        PriceHistory.objects.filter(variant=variant)
        .order_by("valid_from")
        .values_list("amount", flat=True)
    )
    assert amounts == [Decimal("1000.00"), Decimal("1100.00"), Decimal("1250.00")]
    # Two closed rows + one open row.
    assert (
        PriceHistory.objects.filter(variant=variant, valid_to__isnull=False).count()
        == 2
    )
