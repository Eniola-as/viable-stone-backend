"""The development ``seed_demo`` command builds a full, coherent demo shop.

The command expands an *existing* ``DEMO`` branch (owner ``demo-owner``,
cashier ``demo-cashier``) with a realistic dataset built entirely through the
real domain services, so stock movements, weighted-average cost, reports,
notifications and audit rows are all correct. An ordinary run preserves
everything that already exists (user ids, password hashes, MFA devices,
recovery codes, audit history, manually created products and uploaded images,
and every non-DEMO branch). ``--reset`` is an explicit, confirmed teardown of
the DEMO branch only.
"""

from __future__ import annotations

import io
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum
from django.utils import timezone

pytestmark = pytest.mark.django_db

_OWNER_PW = "demo-owner-pw-123"
_CASHIER_PW = "demo-cashier-pw-123"
_ENV = {
    "DEMO_OWNER_PASSWORD": _OWNER_PW,
    "DEMO_EMPLOYEE_PASSWORD": _CASHIER_PW,
}

WAC_SKU = "DUL-WEA-WHT-20L-M"


# --------------------------------------------------------------------------- #
# helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #


def _png_bytes(colour=(10, 60, 140)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


def _all_media_files() -> set[str]:
    from django.conf import settings

    root = Path(settings.PRIVATE_MEDIA_ROOT)
    if not root.exists():
        return set()
    return {
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def demo_env(settings, monkeypatch):
    settings.DEBUG = True
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


@pytest.fixture
def seed(demo_env):
    def _run(*args):
        call_command("seed_demo", *args)
        from apps.accounts.models import Branch

        return Branch.objects.get(code="DEMO")

    return _run


@pytest.fixture
def seeded(seed):
    return seed()


# --------------------------------------------------------------------------- #
# 1. production refusal                                                        #
# --------------------------------------------------------------------------- #


def test_refuses_to_run_under_production_settings(monkeypatch, settings):
    settings.DEBUG = False
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(CommandError, match="refuses to run"):
        call_command("seed_demo")


def test_refuses_when_debug_is_false(monkeypatch, settings):
    settings.DEBUG = False
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(CommandError):
        call_command("seed_demo")


# --------------------------------------------------------------------------- #
# 2. credentials only when accounts are missing / explicitly reset             #
# --------------------------------------------------------------------------- #


def test_no_passwords_needed_once_the_demo_accounts_exist(seed, demo_env):
    seed()  # creates the accounts (env passwords present)
    demo_env.delenv("DEMO_OWNER_PASSWORD", raising=False)
    demo_env.delenv("DEMO_EMPLOYEE_PASSWORD", raising=False)
    seed()  # must NOT raise — nothing to create, no password reset asked for


def test_passwords_required_when_an_account_is_missing(demo_env):
    demo_env.delenv("DEMO_OWNER_PASSWORD", raising=False)
    demo_env.delenv("DEMO_EMPLOYEE_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="DEMO_OWNER_PASSWORD"):
        call_command("seed_demo")


def test_reset_passwords_requires_credentials(seed, demo_env):
    seed()
    demo_env.delenv("DEMO_OWNER_PASSWORD", raising=False)
    demo_env.delenv("DEMO_EMPLOYEE_PASSWORD", raising=False)
    with pytest.raises(CommandError):
        call_command("seed_demo", "--reset-passwords")


def test_reset_passwords_replaces_the_hash_when_credentials_are_supplied(seed):
    from apps.accounts.models import User

    seed()
    before = User.objects.get(username="demo-owner").password
    call_command("seed_demo", "--reset-passwords")
    after = User.objects.get(username="demo-owner").password
    assert before != after
    assert User.objects.get(username="demo-owner").check_password(_OWNER_PW)


# --------------------------------------------------------------------------- #
# 3. an ordinary run preserves existing accounts                               #
# --------------------------------------------------------------------------- #


def test_ordinary_run_preserves_ids_hashes_mfa_devices_and_recovery_codes(
    seed, demo_env
):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts.models import User
    from apps.accounts.services.recovery import generate_recovery_codes

    seed()
    owner = User.objects.get(username="demo-owner")
    cashier = User.objects.get(username="demo-cashier")
    owner_id, owner_hash = owner.id, owner.password
    cashier_id, cashier_hash = cashier.id, cashier.password

    device = TOTPDevice.objects.create(user=owner, name="primary", confirmed=True)
    generate_recovery_codes(owner)
    codes = owner.recovery_codes.count()

    demo_env.delenv("DEMO_OWNER_PASSWORD", raising=False)
    demo_env.delenv("DEMO_EMPLOYEE_PASSWORD", raising=False)
    seed()  # ordinary re-run, no credentials

    owner.refresh_from_db()
    cashier.refresh_from_db()
    assert (owner.id, owner.password) == (owner_id, owner_hash)
    assert (cashier.id, cashier.password) == (cashier_id, cashier_hash)
    assert TOTPDevice.objects.filter(pk=device.pk, confirmed=True).exists()
    assert owner.recovery_codes.count() == codes


def test_ordinary_run_never_deletes_existing_audit_history(seed):
    from apps.core.models import AuditLog

    seed()
    before = AuditLog.objects.count()
    assert before > 0
    seed()
    assert AuditLog.objects.count() >= before


def test_ordinary_run_repairs_a_drifted_demo_account_branch_without_touching_secrets(
    seed,
):
    """An existing demo account may point at an old / deleted DEMO branch row.
    An ordinary run re-attaches it to the live DEMO branch (so create_sale works)
    while preserving its id, password hash and MFA enrolment."""

    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts.models import User
    from apps.accounts.tests.factories import BranchFactory

    branch = seed()
    cashier = User.objects.get(username="demo-cashier")
    cashier_id, cashier_hash = cashier.id, cashier.password
    device = TOTPDevice.objects.create(
        user=User.objects.get(username="demo-owner"), name="primary", confirmed=True
    )

    # simulate the drift: move the cashier onto some other branch
    cashier.branch = BranchFactory(code="STALE")
    cashier.save(update_fields=["branch"])

    seed()  # ordinary re-run must not raise "cashier does not belong to this branch"

    cashier.refresh_from_db()
    assert cashier.branch_id == branch.id
    assert (cashier.id, cashier.password) == (cashier_id, cashier_hash)
    assert TOTPDevice.objects.filter(pk=device.pk, confirmed=True).exists()
    from apps.sales.models import Sale

    assert Sale.objects.filter(branch=branch, status="COMPLETED").count() >= 4


# --------------------------------------------------------------------------- #
# 4. an ordinary run preserves manual DEMO products and images                 #
# --------------------------------------------------------------------------- #


def test_ordinary_run_preserves_manual_products_and_uploaded_images(seed):
    from apps.catalog.models import Category, Product

    branch = seed()
    category = Category.objects.filter(branch=branch).first()
    manual = Product.objects.create(
        branch=branch, category=category, kind="PAINT", name="Manual Custom Blend"
    )
    manual.image.save("manual-upload.png", ContentFile(_png_bytes()), save=True)
    manual_id, manual_image = manual.id, manual.image.name

    seed()  # second ordinary run

    manual.refresh_from_db()
    assert Product.objects.filter(pk=manual_id).exists()
    assert manual.image.name == manual_image
    assert manual.image.storage.exists(manual_image)


def test_ordinary_run_never_overwrites_a_manual_image_on_a_seeded_product(seed):
    from apps.catalog.models import Product

    branch = seed()
    product = Product.objects.get(branch=branch, name="Paint Brush")
    product.image.save(
        "hand-photo.png", ContentFile(_png_bytes((200, 10, 10))), save=True
    )
    chosen = product.image.name
    assert "demo-seed-" not in chosen

    seed()

    product.refresh_from_db()
    assert product.image.name == chosen  # untouched


# --------------------------------------------------------------------------- #
# 5. a complete, coherent dataset                                             #
# --------------------------------------------------------------------------- #


def test_seeds_a_complete_coherent_dataset(seeded):
    branch = seeded
    from apps.accounts.models import RegisteredDevice, User
    from apps.catalog.models import Brand, Category, Product, ProductVariant
    from apps.finance.models import Expense, ExpenseCategory
    from apps.inventory.models import Restock, RestockStatus, Supplier
    from apps.sales.models import (
        ApprovalRequest,
        ApprovalStatus,
        Customer,
        Payment,
        Sale,
        SaleReturn,
        SaleStatus,
    )

    assert User.objects.filter(branch=branch, role="OWNER").count() == 1
    assert User.objects.filter(branch=branch, role="EMPLOYEE").count() == 1

    assert Category.objects.filter(branch=branch).count() == 6
    assert Brand.objects.filter(branch=branch).count() == 5
    assert Product.objects.filter(branch=branch).count() >= 12
    assert ProductVariant.objects.filter(product__branch=branch).count() >= 25

    # required catalogue shape
    assert Product.objects.filter(branch=branch, is_active=False).exists()
    assert ProductVariant.objects.filter(
        product__branch=branch, is_active=False
    ).exists()
    assert Product.objects.filter(
        branch=branch, brand__isnull=True, is_active=True
    ).exists()
    assert Product.objects.filter(branch=branch, kind="PAINT", is_active=True).exists()
    assert Product.objects.filter(
        branch=branch, kind="EQUIPMENT", is_active=True
    ).exists()

    # every active variant is priced and every variant has opening stock
    from apps.catalog.selectors import current_price

    for variant in ProductVariant.objects.filter(
        product__branch=branch, is_active=True, product__is_active=True
    ):
        assert current_price(variant) is not None, variant.sku

    assert Supplier.objects.filter(branch=branch, is_active=True).count() >= 2
    assert Supplier.objects.filter(branch=branch, is_active=False).count() >= 1
    assert Restock.objects.filter(
        branch=branch, status=RestockStatus.CONFIRMED
    ).exists()
    assert Restock.objects.filter(branch=branch, status=RestockStatus.DRAFT).exists()

    assert Customer.objects.filter(branch=branch).count() >= 1
    completed = Sale.objects.filter(branch=branch, status=SaleStatus.COMPLETED)
    assert completed.count() >= 4
    assert completed.filter(customer__isnull=True).exists()  # a walk-in
    assert completed.filter(customer__isnull=False).exists()  # a registered buyer
    # the four payment styles
    assert Payment.objects.filter(sale__branch=branch, method="CASH").exists()
    assert (
        Payment.objects.filter(sale__branch=branch, method="TRANSFER")
        .exclude(reference="")
        .exists()
    )
    assert Payment.objects.filter(sale__branch=branch, method="POS").exists()
    assert Sale.objects.filter(branch=branch, change_due__gt=0).exists()

    # discounts / approvals
    assert ApprovalRequest.objects.filter(
        branch=branch, request_type="DISCOUNT", status=ApprovalStatus.PENDING
    ).exists()
    assert ApprovalRequest.objects.filter(
        branch=branch, status__in=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
    ).exists()

    # returns
    assert SaleReturn.objects.filter(branch=branch).exists()
    assert ApprovalRequest.objects.filter(
        branch=branch, request_type="RETURN", status=ApprovalStatus.PENDING
    ).exists()

    # expenses
    assert ExpenseCategory.objects.filter(branch=branch).count() >= 4
    assert Expense.objects.filter(branch=branch, is_voided=False).count() >= 2
    assert Expense.objects.filter(branch=branch, is_voided=True).count() >= 1

    # notifications generated by the real workflow
    from apps.notifications.models import Notification

    assert Notification.objects.filter(branch=branch).exists()

    # a demo device may exist, but never an active offline authorization
    assert RegisteredDevice.objects.filter(branch=branch).exists()


# --------------------------------------------------------------------------- #
# 6. idempotency                                                              #
# --------------------------------------------------------------------------- #


def test_second_run_is_idempotent(seed):
    branch = seed()
    from apps.catalog.models import PriceHistory, Product, ProductVariant
    from apps.finance.models import Expense
    from apps.inventory.models import StockMovement
    from apps.sales.models import Payment, Sale, SaleItem, SaleReturn

    def snapshot():
        return {
            "products": Product.objects.filter(branch=branch).count(),
            "variants": ProductVariant.objects.filter(product__branch=branch).count(),
            "prices": PriceHistory.objects.filter(
                variant__product__branch=branch
            ).count(),
            "sales": Sale.objects.filter(branch=branch).count(),
            "sale_items": SaleItem.objects.filter(sale__branch=branch).count(),
            "payments": Payment.objects.filter(sale__branch=branch).count(),
            "movements": StockMovement.objects.filter(branch=branch).count(),
            "returns": SaleReturn.objects.filter(branch=branch).count(),
            "expenses": Expense.objects.filter(branch=branch).count(),
        }

    first = snapshot()
    seed()
    seed()
    assert snapshot() == first


# --------------------------------------------------------------------------- #
# 7 + 8. reset scope                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def other_branch(db):
    """A non-DEMO branch with its own owner, product and completed sale."""

    from decimal import Decimal

    from apps.accounts.tests.factories import BranchFactory, OwnerFactory
    from apps.catalog.services.pricing import set_active_price
    from apps.catalog.tests.factories import ProductVariantFactory
    from apps.inventory.services.stock import open_stock
    from apps.sales.services.sales import CartLine, PaymentLine, create_sale

    branch = BranchFactory(code="LIVE", name="Real Shop")
    owner = OwnerFactory(username="real-owner", branch=branch)
    variant = ProductVariantFactory(product__branch=branch, sku="LIVE-SKU-1")
    set_active_price(variant=variant, amount=Decimal("1000.00"), changed_by=owner)
    open_stock(
        branch=branch,
        variant=variant,
        quantity=20,
        unit_cost=Decimal("600.00"),
        created_by=owner,
    )
    sale = create_sale(
        branch=branch,
        cashier=owner,
        cart=[CartLine(variant_id=variant.id, quantity=1)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal("1000.00"),
                tendered_amount=Decimal("1000.00"),
            )
        ],
        client_sale_id=uuid.uuid4(),
    )
    return {"branch": branch, "owner": owner, "variant": variant, "sale": sale}


def test_reset_rebuilds_only_the_demo_branch(seed):
    from apps.accounts.models import Branch, User
    from apps.sales.models import Sale

    branch = seed()
    old_owner_id = User.objects.get(username="demo-owner").id
    old_sales = set(Sale.objects.filter(branch=branch).values_list("id", flat=True))

    call_command("seed_demo", "--reset", "--yes")

    assert Branch.objects.filter(code="DEMO").count() == 1
    new_branch = Branch.objects.get(code="DEMO")
    # rebuilt from scratch: a fresh owner and fresh sales
    assert User.objects.get(username="demo-owner").id != old_owner_id
    new_sales = set(Sale.objects.filter(branch=new_branch).values_list("id", flat=True))
    assert new_sales and not (new_sales & old_sales)
    assert Sale.objects.filter(branch=new_branch, status="COMPLETED").count() >= 4


def test_reset_requires_an_explicit_confirmation_flag(seed):
    seed()
    with pytest.raises(CommandError, match="--yes"):
        call_command("seed_demo", "--reset")


def test_reset_wipe_handles_protected_relationships(seed):
    """The DEMO branch is full of PROTECT-guarded rows; the wipe must still work."""

    seed()
    call_command("seed_demo", "--reset", "--yes")  # must not raise ProtectedError
    from apps.accounts.models import Branch

    assert Branch.objects.filter(code="DEMO").exists()


def test_non_demo_data_survives_ordinary_seeding(seed, other_branch):
    seed()
    from apps.accounts.models import Branch, User
    from apps.sales.models import Sale

    assert Branch.objects.filter(code="LIVE").exists()
    assert User.objects.filter(username="real-owner").exists()
    assert Sale.objects.filter(id=other_branch["sale"].id).exists()


def test_non_demo_data_survives_a_reset(seed, other_branch):
    seed()
    call_command("seed_demo", "--reset", "--yes")

    from apps.accounts.models import Branch, User
    from apps.inventory.models import InventoryBalance, StockMovement
    from apps.sales.models import Sale

    assert Branch.objects.filter(code="LIVE").exists()
    assert User.objects.filter(username="real-owner").exists()
    assert Sale.objects.filter(id=other_branch["sale"].id).exists()
    live_balance = InventoryBalance.objects.get(
        branch=other_branch["branch"], variant=other_branch["variant"]
    )
    deltas = StockMovement.objects.filter(
        branch=other_branch["branch"], variant=other_branch["variant"]
    ).aggregate(s=Sum("quantity_delta"))["s"]
    assert live_balance.quantity == deltas == 19


# --------------------------------------------------------------------------- #
# 9. stable identifiers -> no duplicate business events                        #
# --------------------------------------------------------------------------- #


def test_stable_ids_prevent_duplicate_sales_receipts_payments_and_movements(seed):
    branch = seed()
    from apps.inventory.models import MovementType, StockMovement
    from apps.sales.models import Payment, Sale

    sales = list(Sale.objects.filter(branch=branch, status="COMPLETED"))
    receipts = [s.receipt_number for s in sales]
    payments = Payment.objects.filter(sale__branch=branch).count()
    sale_moves = StockMovement.objects.filter(
        branch=branch, movement_type=MovementType.SALE
    ).count()
    client_ids = {s.client_sale_id for s in sales}

    seed()

    sales2 = list(Sale.objects.filter(branch=branch, status="COMPLETED"))
    assert len(sales2) == len(sales)
    assert {s.client_sale_id for s in sales2} == client_ids
    assert [s.receipt_number for s in sales2] == receipts
    assert len(receipts) == len(set(receipts))
    assert Payment.objects.filter(sale__branch=branch).count() == payments
    assert (
        StockMovement.objects.filter(
            branch=branch, movement_type=MovementType.SALE
        ).count()
        == sale_moves
    )


def test_seeding_survives_a_preexisting_sale_that_occupies_a_receipt_number(seed):
    """A sale from an earlier seed run (or another branch sharing the DEMO
    receipt prefix) already holds DEMO-<today>-0001 while the DEMO branch's
    per-day counter is still at zero. The seeder must advance past it, not
    collide on the globally-unique receipt_number."""

    import uuid as _uuid
    from decimal import Decimal

    from apps.accounts.tests.factories import BranchFactory, OwnerFactory
    from apps.catalog.services.pricing import set_active_price
    from apps.catalog.tests.factories import ProductVariantFactory
    from apps.inventory.services.stock import open_stock
    from apps.sales.models import ReceiptSequence, Sale
    from apps.sales.services.sales import CartLine, PaymentLine, create_sale

    other = BranchFactory(code="DUP", receipt_prefix="DEMO")  # same prefix!
    o_owner = OwnerFactory(username="dup-owner", branch=other)
    o_variant = ProductVariantFactory(product__branch=other, sku="DUP-1")
    set_active_price(variant=o_variant, amount=Decimal("1000.00"), changed_by=o_owner)
    open_stock(
        branch=other,
        variant=o_variant,
        quantity=10,
        unit_cost=Decimal("600.00"),
        created_by=o_owner,
    )
    pre = create_sale(
        branch=other,
        cashier=o_owner,
        cart=[CartLine(variant_id=o_variant.id, quantity=1)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal("1000.00"),
                tendered_amount=Decimal("1000.00"),
            )
        ],
        client_sale_id=_uuid.uuid4(),
    )
    assert pre.receipt_number.endswith("-0001")

    branch = seed()  # must not raise IntegrityError on receipt_number

    demo_today = Sale.objects.filter(
        branch=branch, receipt_number__startswith=pre.receipt_number[:-4]
    )
    assert demo_today.exists()
    assert not demo_today.filter(receipt_number=pre.receipt_number).exists()
    # every receipt number is still globally unique
    numbers = list(
        Sale.objects.exclude(receipt_number__isnull=True).values_list(
            "receipt_number", flat=True
        )
    )
    assert len(numbers) == len(set(numbers))
    assert ReceiptSequence.objects.filter(branch=branch).exists()


# --------------------------------------------------------------------------- #
# 10. restock confirmed exactly once                                          #
# --------------------------------------------------------------------------- #


def test_restock_confirmation_happens_once(seed):
    branch = seed()
    from apps.inventory.models import MovementType, Restock, StockMovement

    confirmed = Restock.objects.filter(branch=branch, status="CONFIRMED")
    ids = set(confirmed.values_list("id", flat=True))
    restock_moves = StockMovement.objects.filter(
        branch=branch, movement_type=MovementType.RESTOCK
    ).count()

    seed()
    seed()

    assert (
        set(
            Restock.objects.filter(branch=branch, status="CONFIRMED").values_list(
                "id", flat=True
            )
        )
        == ids
    )
    assert (
        StockMovement.objects.filter(
            branch=branch, movement_type=MovementType.RESTOCK
        ).count()
        == restock_moves
    )


# --------------------------------------------------------------------------- #
# 11. weighted-average cost demonstration                                     #
# --------------------------------------------------------------------------- #


def test_weighted_average_cost_example_is_exactly_27500(seed):
    branch = seed()
    from apps.inventory.models import InventoryBalance

    balance = InventoryBalance.objects.get(branch=branch, variant__sku=WAC_SKU)
    assert balance.quantity == 20
    assert balance.average_unit_cost == Decimal("27500.00")

    seed()  # still correct after a re-run
    balance.refresh_from_db()
    assert balance.average_unit_cost == Decimal("27500.00")


# --------------------------------------------------------------------------- #
# 12-14. inventory invariants                                                 #
# --------------------------------------------------------------------------- #


def test_normal_low_and_out_of_stock_examples_all_exist(seeded):
    from apps.inventory.models import InventoryBalance
    from apps.notifications.services.stock_alerts import classify_stock

    buckets = {
        classify_stock(b.quantity, b.variant.low_stock_level)
        for b in InventoryBalance.objects.select_related("variant").filter(
            branch=seeded
        )
    }
    assert {"OK", "LOW", "OUT"} <= buckets


def test_every_inventory_balance_equals_the_sum_of_its_movements(seeded):
    from apps.inventory.models import InventoryBalance, StockMovement

    for balance in InventoryBalance.objects.select_related("variant").filter(
        branch=seeded
    ):
        delta = (
            StockMovement.objects.filter(
                branch=seeded, variant=balance.variant
            ).aggregate(s=Sum("quantity_delta"))["s"]
            or 0
        )
        assert balance.quantity == delta, balance.variant.sku


def test_no_inventory_balance_is_negative(seeded):
    from apps.inventory.models import InventoryBalance

    assert not InventoryBalance.objects.filter(branch=seeded, quantity__lt=0).exists()


# --------------------------------------------------------------------------- #
# 15. pending discount draft does not move stock                              #
# --------------------------------------------------------------------------- #


def test_pending_discount_draft_does_not_reduce_stock(seeded):
    from apps.inventory.models import InventoryBalance, StockMovement
    from apps.sales.models import ApprovalStatus, ApprovalType, Sale

    draft = Sale.objects.get(branch=seeded, status__in=["DRAFT", "PENDING_APPROVAL"])
    assert draft.approval_requests.filter(
        request_type=ApprovalType.DISCOUNT, status=ApprovalStatus.PENDING
    ).exists()
    assert not StockMovement.objects.filter(reference_id=draft.id).exists()

    for item in draft.items.all():
        balance = InventoryBalance.objects.get(branch=seeded, variant=item.variant)
        delta = (
            StockMovement.objects.filter(branch=seeded, variant=item.variant).aggregate(
                s=Sum("quantity_delta")
            )["s"]
            or 0
        )
        assert balance.quantity == delta


# --------------------------------------------------------------------------- #
# 16. returns and refunds                                                     #
# --------------------------------------------------------------------------- #


def test_return_and_refund_examples_have_the_correct_side_effects(seed):
    branch = seed()
    from apps.inventory.models import MovementType, StockMovement
    from apps.sales.models import Refund, SaleReturn, SaleReturnItem

    sr = SaleReturn.objects.filter(branch=branch).first()
    assert sr is not None
    refunds_total = sr.refunds.aggregate(s=Sum("amount"))["s"]
    assert refunds_total == sr.total
    assert Refund.objects.filter(sale_return=sr).exists()

    # resellable: original price used, stock restored via a RETURN movement
    item = SaleReturnItem.objects.get(sale_return=sr)
    original = item.original_sale_item
    assert item.unit_price_snapshot == original.unit_price_snapshot
    assert StockMovement.objects.filter(
        reference_id=sr.id, movement_type=MovementType.RETURN
    ).exists()

    # original sale + receipt untouched
    sr.original_sale.refresh_from_db()
    assert sr.original_sale.receipt_number
    assert sr.original_sale.status in ("PARTIALLY_RETURNED", "RETURNED")

    refund_count = Refund.objects.filter(sale_return=sr).count()
    return_moves = StockMovement.objects.filter(
        reference_id=sr.id, movement_type=MovementType.RETURN
    ).aggregate(s=Sum("quantity_delta"))["s"]

    seed()  # re-run must not refund or restore twice

    sr.refresh_from_db()
    assert Refund.objects.filter(sale_return=sr).count() == refund_count
    assert (
        StockMovement.objects.filter(
            reference_id=sr.id, movement_type=MovementType.RETURN
        ).aggregate(s=Sum("quantity_delta"))["s"]
        == return_moves
    )


# --------------------------------------------------------------------------- #
# 17. voided expense                                                          #
# --------------------------------------------------------------------------- #


def test_voided_expense_is_preserved_and_excluded_from_net_profit(seeded):
    from apps.finance.models import Expense
    from apps.finance.services.reports import profit_report

    voided = Expense.objects.filter(branch=seeded, is_voided=True)
    assert voided.exists()
    assert all(e.void_reason for e in voided)

    report = profit_report(branch=seeded, period="month")
    active_month = Expense.objects.filter(
        branch=seeded,
        is_voided=False,
        expense_date__gte=timezone.localdate().replace(day=1),
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    assert Decimal(report["expenses_total"]) == active_month
    # the voided amount is not part of it
    for e in voided:
        assert e.expense_date  # still on record


# --------------------------------------------------------------------------- #
# 18. seeded image streams through the authenticated endpoint                  #
# --------------------------------------------------------------------------- #


def test_seeded_image_streams_through_the_authenticated_endpoint(seeded, login_as):
    from apps.accounts.models import User
    from apps.catalog.models import Product

    product = (
        Product.objects.filter(branch=seeded).exclude(image="").order_by("name").first()
    )
    assert product is not None
    assert "demo-seed-" in os.path.basename(product.image.name)

    owner = User.objects.get(username="demo-owner")
    res = login_as(owner).get(f"/api/v1/products/{product.id}/image/")
    assert res.status_code == 200
    body = (
        b"".join(res.streaming_content)
        if hasattr(res, "streaming_content")
        else res.content
    )
    assert body
    assert res["Content-Type"].startswith("image/")


# --------------------------------------------------------------------------- #
# 19-20. offline + MFA posture                                                #
# --------------------------------------------------------------------------- #


def test_no_active_offline_authorization_is_left_behind(seeded):
    from apps.accounts.models import (
        OfflineAuthorizationStatus,
        OfflineDeviceAuthorization,
    )

    assert not OfflineDeviceAuthorization.objects.filter(
        branch=seeded, status=OfflineAuthorizationStatus.ACTIVE
    ).exists()


def test_owner_remains_mfa_required(seeded):
    from apps.accounts.models import User

    assert User.objects.get(username="demo-owner").mfa_required is True


# --------------------------------------------------------------------------- #
# 21. a failure rolls the whole run back                                      #
# --------------------------------------------------------------------------- #


def test_command_failure_rolls_back_every_change(demo_env, monkeypatch):
    from apps.accounts.models import Branch
    from apps.core.management.commands import seed_demo

    def boom(self, *args, **kwargs):
        raise RuntimeError("seed exploded late")

    monkeypatch.setattr(seed_demo.Command, "_offline_device", boom, raising=True)

    media_before = _all_media_files()
    with pytest.raises(RuntimeError, match="exploded"):
        call_command("seed_demo")

    assert not Branch.objects.filter(code="DEMO").exists()
    assert _all_media_files() == media_before  # no orphan seed images


# --------------------------------------------------------------------------- #
# 22. image cleanup on reset only touches demo-owned files                     #
# --------------------------------------------------------------------------- #


def test_reset_image_cleanup_never_removes_non_demo_files(seed):
    seed()
    keep = default_storage.save(
        "products/keep-me-please.png", ContentFile(_png_bytes((0, 0, 0)))
    )
    seed_images_before = {n for n in _all_media_files() if "demo-seed-" in n}
    assert seed_images_before

    call_command("seed_demo", "--reset", "--yes")

    assert default_storage.exists(keep)
    seed_images_after = {n for n in _all_media_files() if "demo-seed-" in n}
    # rebuilt cleanly — the old seed images were removed, not left as duplicates
    assert len(seed_images_after) == len(seed_images_before)
