"""Fixtures shared by the Stage 18 acceptance suites."""

from __future__ import annotations

import importlib
import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import clear_url_caches


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    """Acceptance suites hit many endpoints; start each test with clean
    throttle counters so a global baseline never causes a flaky 429."""

    cache.clear()
    yield
    cache.clear()


@contextmanager
def reloaded_urlconf(**setting_overrides):
    """Temporarily apply settings and rebuild the root URLconf.

    Needed for tests that assert routing that is decided at import time
    (e.g. whether ``/admin/`` is mounted).
    """

    from django.test import override_settings

    urlconf = settings.ROOT_URLCONF
    api_urls = "config.api_urls"
    with override_settings(**setting_overrides):
        clear_url_caches()
        importlib.reload(importlib.import_module(api_urls))
        importlib.reload(importlib.import_module(urlconf))
        try:
            yield
        finally:
            pass
    clear_url_caches()
    importlib.reload(importlib.import_module(api_urls))
    importlib.reload(importlib.import_module(urlconf))


@pytest.fixture
def stocked(db, branch, owner):
    """A priced, stocked variant plus a helper to make more."""

    from apps.catalog.services.pricing import set_active_price
    from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
    from apps.inventory.services.stock import open_stock

    def _make(*, sku, price="1000.00", qty=50, cost="600.00", low=5, **kw):
        variant = ProductVariantFactory(
            product=ProductFactory(branch=branch, is_active=True),
            sku=sku,
            is_active=True,
            low_stock_level=low,
            **kw,
        )
        set_active_price(variant=variant, amount=Decimal(str(price)), changed_by=owner)
        if qty:
            open_stock(
                branch=branch,
                variant=variant,
                quantity=qty,
                unit_cost=Decimal(str(cost)),
                created_by=owner,
            )
        return variant

    return _make


@pytest.fixture
def new_uuid():
    return lambda: str(uuid.uuid4())
