"""Development-only demo data for the Viable Stone paint shop.

Expands the existing ``DEMO`` branch (owner ``demo-owner``, cashier
``demo-cashier``) into a realistic full-shop dataset — catalogue, images,
prices, opening stock, weighted-average-cost restocks, suppliers, customers,
completed sales with every payment style, a pending owner-approved discount,
returns and refunds, expenses (one voided), stock counts and an owner
adjustment, plus the notifications and audit rows those workflows raise.

Design rules
------------
* **Development only.** Refuses to run under production settings or with
  ``DEBUG=False``.
* **Atomic.** The whole run is one transaction; a failure rolls everything back
  (seeded image files written during the run are removed on failure too).
* **Idempotent.** Every business event is keyed by a deterministic id
  (``uuid5``) or a stable natural key, and is created through the real domain
  service, so a second run adds nothing — no duplicate sales, receipts,
  payments, stock movements, restock confirmations, refunds or voids.
* **Preserving.** An ordinary run never calls ``set_password`` for an existing
  account and never touches existing user ids, MFA devices, recovery codes,
  audit history, manually created DEMO products, manually uploaded images, or
  any non-DEMO branch.
* Passwords are required **only** when a demo account must be created or
  ``--reset-passwords`` is given. They come from ``DEMO_OWNER_PASSWORD`` /
  ``DEMO_EMPLOYEE_PASSWORD`` or, with ``--interactive``, a secure prompt — never
  from the repository.
* ``--reset`` is a confirmed, DEMO-only teardown (``--yes`` outside a TTY). It
  deletes the DEMO branch and its data in dependency order without weakening any
  production foreign-key protection, and removes only the images it seeded.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from getpass import getpass

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

DEMO_BRANCH_CODE = "DEMO"
SEED_IMAGE_MARKER = "demo-seed-"

# Deterministic id space: the same logical event gets the same id on every run.
_SEED_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://viable-stone.local/demo-seed/v1")


def seed_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(_SEED_NS, key)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --------------------------------------------------------------------------- #
# Seed definitions — data only, no behaviour                                  #
# --------------------------------------------------------------------------- #

CATEGORIES = [
    "Interior Paint",
    "Exterior Paint",
    "Primers & Undercoats",
    "Wood & Metal Finishes",
    "Painting Tools",
    "Surface Preparation",
]

BRANDS = ["Dulux", "Berger", "Finecoat", "Meyer", "ProTool"]


@dataclass(frozen=True)
class ProductSpec:
    key: str
    name: str
    kind: str  # "PAINT" | "EQUIPMENT"
    category: str
    brand: str | None  # None -> brandless (proves brand is optional)
    is_active: bool = True


@dataclass(frozen=True)
class VariantSpec:
    sku: str
    product: str  # ProductSpec.key
    colour: str
    size: str
    finish: str
    price: str
    low_stock_level: int
    open_qty: int
    open_cost: str
    barcode: str | None = None
    is_active: bool = True


_INACTIVE_PRODUCTS = {"TEX"}
_INACTIVE_VARIANTS = {"MEY-GLO-RED-1L-G"}
_VARIANT_BARCODES = {
    "DUL-WEA-WHT-20L-M": "6155000000017",
    "DUL-WEA-GRY-20L-M": "6155000000024",
    "DUL-INT-WHT-4L-M": "6155000000031",
    "MEY-GLO-BLK-1L-G": "6155000000055",
    "TOOL-ROLLER-9IN": "6155000000062",
    "PREP-TAPE-24MM": "6155000000079",
    "FIN-THIN-4L": "6155000000086",
}

# fmt: off
# key, name, kind, category, brand  (brand None -> brandless)
_PRODUCT_ROWS = (
    ("WEA",     "Weathershield Exterior Paint",  "PAINT",     "Exterior Paint",        "Dulux"),
    ("INT",     "Vinyl Matt Interior Paint",     "PAINT",     "Interior Paint",        "Dulux"),
    ("LUX",     "Luxury Emulsion",               "PAINT",     "Interior Paint",        "Berger"),
    ("FIN_EMU", "Finecoat Emulsion",             "PAINT",     "Interior Paint",        "Finecoat"),
    ("ALK",     "Alkali Resistant Primer",       "PAINT",     "Primers & Undercoats",  "Berger"),
    ("GLO",     "High Gloss Enamel",             "PAINT",     "Wood & Metal Finishes", "Meyer"),
    ("ROX",     "Red Oxide Primer",              "PAINT",     "Primers & Undercoats",  "Meyer"),
    ("ROLLER",  "Paint Roller Set",              "EQUIPMENT", "Painting Tools",        "ProTool"),
    ("BRUSH",   "Paint Brush",                   "EQUIPMENT", "Painting Tools",        "ProTool"),
    ("TAPE",    "Masking Tape",                  "EQUIPMENT", "Surface Preparation",   None),
    ("SCRAPER", "Wall Scraper",                  "EQUIPMENT", "Surface Preparation",   "ProTool"),
    ("THIN",    "Paint Thinner",                 "PAINT",     "Wood & Metal Finishes", "Finecoat"),
    ("TEX",     "Discontinued Textured Coating", "PAINT",     "Exterior Paint",        "Berger"),  # inactive
)

# sku, product, colour, size, finish, price, low_stock_level, open_qty, open_cost
_VARIANT_ROWS = (
    ("DUL-WEA-WHT-20L-M", "WEA",     "White",     "20 litres",  "Matte", "48000.00",  4, 10, "25000.00"),
    ("DUL-WEA-GRY-20L-M", "WEA",     "Grey",      "20 litres",  "Matte", "49000.00",  4, 15, "26000.00"),
    ("DUL-WEA-WHT-4L-M",  "WEA",     "White",     "4 litres",   "Matte", "12500.00",  6, 12,  "6500.00"),
    ("DUL-INT-WHT-4L-M",  "INT",     "White",     "4 litres",   "Matte",  "9500.00", 10, 30,  "6000.00"),
    ("DUL-INT-MAG-4L-M",  "INT",     "Magnolia",  "4 litres",   "Matte",  "9500.00",  8, 20,  "6000.00"),
    ("DUL-INT-WHT-1L-M",  "INT",     "White",     "1 litre",    "Matte",  "3200.00", 12, 40,  "2000.00"),
    ("BER-LUX-WHT-20L-S", "LUX",     "White",     "20 litres",  "Silk",  "42000.00",  5, 12, "26000.00"),
    ("BER-LUX-IVY-20L-S", "LUX",     "Ivory",     "20 litres",  "Silk",  "42000.00",  5,  8, "25000.00"),
    ("BER-LUX-WHT-4L-S",  "LUX",     "White",     "4 litres",   "Silk",  "12000.00",  8, 25,  "7500.00"),
    ("FIN-EMU-WHT-20L-M", "FIN_EMU", "White",     "20 litres",  "Matte", "30000.00",  6, 14, "18000.00"),
    ("FIN-EMU-CRM-4L-M",  "FIN_EMU", "Cream",     "4 litres",   "Matte",  "8000.00",  8, 22,  "5000.00"),
    ("BER-ALK-WHT-20L",   "ALK",     "White",     "20 litres",  "",      "26000.00",  4, 16, "15000.00"),
    ("BER-ALK-WHT-4L",    "ALK",     "White",     "4 litres",   "",       "8500.00",  6, 18,  "5200.00"),
    ("MEY-GLO-BLK-1L-G",  "GLO",     "Black",     "1 litre",    "Gloss",  "4500.00", 10, 30,  "2700.00"),
    ("MEY-GLO-WHT-1L-G",  "GLO",     "White",     "1 litre",    "Gloss",  "4500.00", 10, 28,  "2700.00"),
    ("MEY-GLO-RED-1L-G",  "GLO",     "Red",       "1 litre",    "Gloss",  "4800.00",  6, 10,  "2900.00"),  # inactive
    ("MEY-ROX-RED-4L",    "ROX",     "Red Oxide", "4 litres",   "",       "7000.00",  5, 10,  "4200.00"),
    ("TOOL-ROLLER-9IN",   "ROLLER",  "",          "9 inch",     "",       "2500.00", 15, 40,  "1200.00"),
    ("TOOL-ROLLER-4IN",   "ROLLER",  "",          "4 inch",     "",       "1800.00", 20, 35,   "900.00"),
    ("TOOL-BRUSH-2IN",    "BRUSH",   "",          "2 inch",     "",       "1500.00", 25, 28,   "900.00"),
    ("TOOL-BRUSH-3IN",    "BRUSH",   "",          "3 inch",     "",       "1900.00", 25, 45,  "1150.00"),
    ("PREP-TAPE-24MM",    "TAPE",    "",          "24mm x 25m", "",       "1200.00", 30, 60,   "700.00"),
    ("PREP-TAPE-48MM",    "TAPE",    "",          "48mm x 25m", "",       "2000.00", 20, 40,  "1200.00"),
    ("TOOL-SCRAPER-STD",  "SCRAPER", "",          "Standard",   "",       "2200.00", 10, 24,  "1300.00"),
    ("FIN-THIN-1L",       "THIN",    "",          "1 litre",    "",       "3500.00", 12,  4,  "2100.00"),
    ("FIN-THIN-4L",       "THIN",    "",          "4 litres",   "",      "12000.00",  8, 16,  "7500.00"),
    ("BER-TEX-WHT-20L",   "TEX",     "White",     "20 litres",  "Textured", "38000.00", 0, 6, "24000.00"),
)
# fmt: on
PRODUCTS: tuple[ProductSpec, ...] = tuple(
    ProductSpec(key, name, kind, category, brand, key not in _INACTIVE_PRODUCTS)
    for key, name, kind, category, brand in _PRODUCT_ROWS
)
VARIANTS: tuple[VariantSpec, ...] = tuple(
    VariantSpec(
        *row[:9],
        barcode=_VARIANT_BARCODES.get(row[0]),
        is_active=row[0] not in _INACTIVE_VARIANTS,
    )
    for row in _VARIANT_ROWS
)
WAC_SKU = "DUL-WEA-WHT-20L-M"  # 10 @ 25,000 opening + 10 @ 30,000 restock -> 27,500


@dataclass(frozen=True)
class SaleSpec:
    key: str
    lines: tuple[tuple[str, int], ...]
    payment: str  # exact_cash | cash_change | transfer | split
    customer: bool
    days_ago: int


SALES: tuple[SaleSpec, ...] = (
    # exact cash — registered customer, today; drops the 2" brush into LOW stock
    SaleSpec("exact-cash", (("TOOL-BRUSH-2IN", 5),), "exact_cash", True, 0),
    # cash with change — walk-in; sells the 1L thinner down to zero (OUT)
    SaleSpec("cash-change", (("FIN-THIN-1L", 4),), "cash_change", False, 3),
    # transfer with a reference — walk-in
    SaleSpec("transfer-ref", (("DUL-INT-WHT-4L-M", 2),), "transfer", False, 12),
    # split payment (cash + POS) — walk-in
    SaleSpec("split-pos", (("BER-LUX-WHT-20L-S", 1),), "split", False, 20),
    # a fifth completed sale that the resellable return is taken against, so the
    # four payment-style demos above all stay COMPLETED
    SaleSpec("return-source", (("DUL-INT-MAG-4L-M", 2),), "exact_cash", False, 15),
)

# expenses: (category, description, amount, days_ago)
EXPENSES: tuple[tuple[str, str, str, int], ...] = (
    ("Utilities", "DEMO: monthly electricity and water bill", "18500.00", 5),
    ("Transport", "DEMO: delivery van fuel for the week", "9000.00", 2),
    ("Rent", "DEMO: shop rent contribution", "75000.00", 12),
    ("Maintenance", "DEMO: repair of the tinting machine", "12000.00", 8),
)
VOIDED_EXPENSE = ("Transport", "DEMO: duplicate fuel claim entered twice", "9000.00", 6)


_THUMBNAIL_CACHE: bytes | None = None


def demo_thumbnail() -> bytes:
    """A tiny in-memory blue-and-white PNG. No downloaded or copyrighted art."""

    global _THUMBNAIL_CACHE
    if _THUMBNAIL_CACHE is None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 200), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 190, 190), fill=(28, 78, 156))
        draw.rectangle((34, 86, 166, 118), fill=(255, 255, 255))
        draw.ellipse((78, 40, 122, 84), fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        _THUMBNAIL_CACHE = buffer.getvalue()
    return _THUMBNAIL_CACHE


# --------------------------------------------------------------------------- #
# Command                                                                     #
# --------------------------------------------------------------------------- #


class Command(BaseCommand):
    help = "Populate development demo data (never runs under production settings)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Destructive: delete the DEMO branch and its data, then rebuild.",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Re-set demo account passwords (needs credentials).",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Prompt for demo passwords / reset confirmation instead of env.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --reset without a prompt (required outside a TTY).",
        )

    # -- orchestration -------------------------------------------------- #

    def handle(self, *args, **options):
        self._guard_environment()
        self._written_images: list[tuple[str, object]] = []
        self._summary: dict = {}
        try:
            with transaction.atomic():
                self._seed(options)
        except Exception:
            self._rollback_written_images()
            raise
        self._print_summary()

    def _seed(self, options):
        if options["reset"]:
            self._confirm_reset(options)
            self._wipe()

        branch = self._branch()
        owner, cashier = self._users(branch, options)

        categories = self._categories(branch)
        brands = self._brands(branch)
        products = self._products(branch, categories, brands)
        variants = self._variants(products)
        self._images(products)

        self._prices(variants, owner)
        self._opening_stock(branch, variants, owner)

        suppliers = self._suppliers(branch)
        self._restocks(branch, suppliers, owner)

        customer = self._customer(branch)
        self._sales(branch, cashier, customer)
        self._discount_draft(branch, cashier)
        self._returns(branch, owner, cashier)

        self._expenses(branch, owner)
        self._stock_counts_and_adjustment(branch, owner)
        self._offline_device(branch, owner)

        self._collect_summary(branch)

    # -- guards ------------------------------------------------------- #

    def _guard_environment(self):
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if module.endswith(".production") or not settings.DEBUG:
            raise CommandError(
                "seed_demo refuses to run outside DEBUG / development settings."
            )

    def _confirm_reset(self, options):
        if options["yes"]:
            return
        if options["interactive"] and sys.stdin.isatty():
            answer = input(
                "This DELETES the entire DEMO branch — users, MFA enrolment, "
                "sales, stock history and seeded images. Type 'DELETE DEMO' to "
                "proceed: "
            )
            if answer.strip() != "DELETE DEMO":
                raise CommandError("Reset aborted.")
            return
        raise CommandError(
            "--reset is destructive: it deletes the DEMO branch, its user "
            "accounts and their MFA enrolment. Re-run with --yes to confirm."
        )

    def _read_passwords(self, interactive: bool) -> tuple[str, str]:
        if interactive:
            owner_pw = getpass("Demo owner password: ")
            cashier_pw = getpass("Demo cashier password: ")
        else:
            owner_pw = os.environ.get("DEMO_OWNER_PASSWORD", "")
            cashier_pw = os.environ.get("DEMO_EMPLOYEE_PASSWORD", "")
        if not owner_pw or not cashier_pw:
            raise CommandError(
                "Set DEMO_OWNER_PASSWORD and DEMO_EMPLOYEE_PASSWORD (or pass "
                "--interactive). They are never stored in the repository."
            )
        if len(owner_pw) < 10 or len(cashier_pw) < 10:
            raise CommandError("Demo passwords must be at least 10 characters.")
        return owner_pw, cashier_pw

    # -- accounts (preserving) -------------------------------------- #

    def _branch(self):
        from apps.accounts.models import Branch

        branch, _ = Branch.objects.get_or_create(
            code=DEMO_BRANCH_CODE,
            defaults={"name": "Demo Paint Shop", "receipt_prefix": "DEMO"},
        )
        return branch

    def _users(self, branch, options):
        from apps.accounts.models import User

        owner_exists = User.objects.filter(username="demo-owner").exists()
        cashier_exists = User.objects.filter(username="demo-cashier").exists()
        reset_pw = options["reset_passwords"]
        need_passwords = (not owner_exists) or (not cashier_exists) or reset_pw

        owner_pw = cashier_pw = None
        if need_passwords:
            owner_pw, cashier_pw = self._read_passwords(options["interactive"])

        owner = self._user(
            "demo-owner",
            "OWNER",
            branch,
            owner_pw,
            create=not owner_exists,
            reset_pw=reset_pw,
        )
        cashier = self._user(
            "demo-cashier",
            "EMPLOYEE",
            branch,
            cashier_pw,
            create=not cashier_exists,
            reset_pw=reset_pw,
        )
        return owner, cashier

    def _user(self, username, role, branch, password, *, create, reset_pw):
        from apps.accounts.models import MFA_ROLES, User

        if create:
            user = User(
                username=username,
                role=role,
                branch=branch,
                is_active=True,
                must_change_password=False,
            )
            user.set_password(password)
            user.save()  # User.save() forces mfa_required for OWNER
            return user

        # Existing account: preserve the id, password hash, MFA enrolment and
        # recovery codes. Only repair the structural fields that must be right
        # for this to be a working DEMO account (e.g. the branch FK may point at
        # an old, deleted DEMO branch row). Never call set_password unless the
        # caller explicitly asked with --reset-passwords.
        user = User.objects.get(username=username)
        fields: list[str] = []
        if user.branch_id != branch.id:
            user.branch = branch
            fields.append("branch")
        if user.role != role:
            user.role = role
            fields.append("role")
        if not user.is_active:
            user.is_active = True
            fields.append("is_active")
        if role in MFA_ROLES and not user.mfa_required:
            user.mfa_required = True
            fields.append("mfa_required")
        if reset_pw:
            user.set_password(password)
            fields.append("password")
        if fields:
            user.save(update_fields=fields)
        return user

    # -- catalogue ------------------------------------------------- #

    @staticmethod
    def _get_or_create_named(model, branch, name):
        """Case-insensitive get-or-create.

        Category / Brand / ExpenseCategory each carry a case-insensitive unique
        constraint on ``(branch, name)``. A plain ``get_or_create`` matches only
        the exact casing, so a manually created ``"dulux"`` would make us try to
        insert ``"Dulux"`` and hit the constraint. Match on ``name__iexact``.
        """

        obj = model.objects.filter(branch=branch, name__iexact=name).first()
        if obj is None:
            obj = model.objects.create(branch=branch, name=name)
        return obj

    def _categories(self, branch) -> dict:
        from apps.catalog.models import Category

        return {n: self._get_or_create_named(Category, branch, n) for n in CATEGORIES}

    def _brands(self, branch) -> dict:
        from apps.catalog.models import Brand

        return {n: self._get_or_create_named(Brand, branch, n) for n in BRANDS}

    def _products(self, branch, categories, brands) -> dict:
        from apps.catalog.models import Product

        out = {}
        for spec in PRODUCTS:
            product, _ = Product.objects.get_or_create(
                branch=branch,
                name=spec.name,
                defaults={
                    "category": categories[spec.category],
                    "brand": brands[spec.brand] if spec.brand else None,
                    "kind": spec.kind,
                    "is_active": spec.is_active,
                },
            )
            out[spec.key] = product
        return out

    def _variants(self, products) -> dict:
        from apps.catalog.models import ProductVariant

        out = {}
        for spec in VARIANTS:
            variant, _ = ProductVariant.objects.get_or_create(
                sku=spec.sku,
                defaults={
                    "product": products[spec.product],
                    "colour": spec.colour,
                    "size": spec.size,
                    "finish": spec.finish,
                    "barcode": spec.barcode,
                    "low_stock_level": spec.low_stock_level,
                    "is_active": spec.is_active,
                },
            )
            out[spec.sku] = variant
        return out

    def _images(self, products):
        for spec in PRODUCTS:
            product = products[spec.key]
            name = product.image.name if product.image else ""
            if name and SEED_IMAGE_MARKER in posixpath.basename(name):
                continue  # already seeded — idempotent
            if name:
                continue  # a manually uploaded image — never overwrite
            filename = f"{SEED_IMAGE_MARKER}{_slug(spec.name)}.png"
            product.image.save(filename, ContentFile(demo_thumbnail()), save=True)
            self._written_images.append((product.image.name, product.image.storage))

    # -- prices & opening stock ---------------------------------- #

    def _prices(self, variants, owner):
        from apps.catalog.services.pricing import set_active_price

        for spec in VARIANTS:
            set_active_price(
                variant=variants[spec.sku],
                amount=Decimal(spec.price),
                changed_by=owner,
            )

    def _opening_stock(self, branch, variants, owner):
        from apps.inventory.models import StockMovement
        from apps.inventory.services.stock import open_stock

        for spec in VARIANTS:
            variant = variants[spec.sku]
            # Movement history — not "quantity == 0" — proves opening was set.
            if StockMovement.objects.filter(branch=branch, variant=variant).exists():
                continue
            open_stock(
                branch=branch,
                variant=variant,
                quantity=spec.open_qty,
                unit_cost=Decimal(spec.open_cost),
                created_by=owner,
            )

    # -- suppliers & restocks ---------------------------------- #

    def _suppliers(self, branch) -> dict:
        from apps.inventory.models import Supplier

        specs = [
            ("Dulux Distributors Ltd", "Amaka Obi", "+2348030000001", True),
            ("Berger Depot Nigeria", "Yusuf Bello", "+2348030000002", True),
            ("Old Coatings Supplier", "", "+2348030000003", False),
        ]
        out = {}
        for name, contact, phone, active in specs:
            supplier, _ = Supplier.objects.get_or_create(
                branch=branch,
                name=name,
                defaults={
                    "contact_name": contact,
                    "phone": phone,
                    "is_active": active,
                },
            )
            out[name] = supplier
        return out

    def _restocks(self, branch, suppliers, owner):
        from apps.inventory.models import Restock, RestockItem, RestockStatus
        from apps.inventory.services.restock import (
            confirm_restock,
            restock_purchase_total,
        )

        today = timezone.localdate()

        def build(invoice, supplier, date, items, *, confirm):
            restock, _ = Restock.objects.get_or_create(
                branch=branch,
                supplier_invoice_number=invoice,
                defaults={"supplier": supplier, "date": date, "created_by": owner},
            )
            if restock.status != RestockStatus.CONFIRMED:
                for sku, qty, cost in items:
                    RestockItem.objects.get_or_create(
                        restock=restock,
                        variant_id=self._variant_id(sku),
                        defaults={"quantity": qty, "unit_cost": Decimal(cost)},
                    )
            if confirm and restock.status != RestockStatus.CONFIRMED:
                confirm_restock(restock=restock, confirmed_by=owner)
            elif restock.status != RestockStatus.CONFIRMED:
                # A draft carries the real purchase total from the outset.
                total = restock_purchase_total(restock)
                if restock.total_cost != total:
                    restock.total_cost = total
                    restock.save(update_fields=["total_cost", "updated_at"])
            return restock

        # Weighted-average-cost demo: opening 10 @ 25,000 then +10 @ 30,000.
        build(
            "DEMO-WAC-2026-001",
            suppliers["Dulux Distributors Ltd"],
            today,
            [(WAC_SKU, 10, "30000.00")],
            confirm=True,
        )
        # A second confirmed restock (other variants) with an invoice reference.
        build(
            "DEMO-GEN-2026-014",
            suppliers["Berger Depot Nigeria"],
            today - timedelta(days=2),
            [("DUL-WEA-WHT-4L-M", 20, "5500.00"), ("BER-LUX-IVY-20L-S", 8, "27000.00")],
            confirm=True,
        )
        # A draft restock left for the owner to review.
        build(
            "DEMO-DRAFT-2026-020",
            suppliers["Dulux Distributors Ltd"],
            today,
            [("MEY-GLO-BLK-1L-G", 12, "4200.00"), ("TOOL-ROLLER-9IN", 30, "800.00")],
            confirm=False,
        )

    def _variant_id(self, sku):
        from apps.catalog.models import ProductVariant

        return ProductVariant.objects.values_list("id", flat=True).get(sku=sku)

    # -- customers & sales ------------------------------------ #

    def _customer(self, branch):
        from apps.sales.models import Customer

        customer, _ = Customer.objects.get_or_create(
            branch=branch,
            name="Chidi Okafor",
            defaults={"phone": "+2348060000010"},
        )
        return customer

    def _payment_plan(self, kind: str, total: Decimal):
        from apps.core.money import to_money
        from apps.sales.services.sales import PaymentLine

        if kind == "exact_cash":
            return [PaymentLine(method="CASH", amount=total, tendered_amount=total)]
        if kind == "cash_change":
            return [
                PaymentLine(
                    method="CASH",
                    amount=total,
                    tendered_amount=to_money(total + Decimal("1000.00")),
                )
            ]
        if kind == "transfer":
            return [
                PaymentLine(method="TRANSFER", amount=total, reference="TRF-DEMO-0007")
            ]
        if kind == "split":
            cash = to_money(total / 2)
            return [
                PaymentLine(method="CASH", amount=cash, tendered_amount=cash),
                PaymentLine(method="POS", amount=to_money(total - cash)),
            ]
        raise ValueError(kind)

    def _heal_receipt_sequences(self, branch, business_dates):
        """Advance the branch's per-day receipt counter past any receipt number
        that already exists for that prefix and date.

        ``receipt_number`` is globally unique, but ``ReceiptSequence`` is only
        per ``(branch, date)``. A sale left by an earlier seed run (or a second
        branch that shares this ``receipt_prefix``) can therefore occupy
        ``DEMO-<date>-0001`` while this branch's counter is still at zero, which
        would make ``create_sale`` collide. Bumping the counter to the real
        high-water mark keeps the numbering monotonic and collision-free.
        """

        from apps.sales.models import ReceiptSequence, Sale

        for date in business_dates:
            prefix = f"{branch.receipt_prefix}-{date:%Y%m%d}-"
            highest = 0
            for number in Sale.objects.filter(
                receipt_number__startswith=prefix
            ).values_list("receipt_number", flat=True):
                match = re.search(r"(\d+)$", number or "")
                if match:
                    highest = max(highest, int(match.group(1)))
            if highest == 0:
                continue
            sequence, _ = ReceiptSequence.objects.get_or_create(
                branch=branch,
                business_date=date,
                defaults={"last_number": highest},
            )
            if sequence.last_number < highest:
                sequence.last_number = highest
                sequence.save(update_fields=["last_number"])

    def _sales(self, branch, cashier, customer):
        from apps.catalog.models import ProductVariant
        from apps.catalog.selectors import current_price
        from apps.core.money import to_money
        from apps.sales.models import Sale
        from apps.sales.services.sales import CartLine, create_sale

        now = timezone.now()
        today = timezone.localdate()
        self._heal_receipt_sequences(
            branch, {today - timedelta(days=spec.days_ago) for spec in SALES}
        )
        for spec in SALES:
            client_sale_id = seed_uuid(f"sale:{spec.key}")
            if Sale.objects.filter(
                branch=branch, client_sale_id=client_sale_id
            ).exists():
                continue
            cart, total = [], Decimal("0.00")
            for sku, qty in spec.lines:
                variant = ProductVariant.objects.get(sku=sku)
                total += current_price(variant).amount * qty
                cart.append(CartLine(variant_id=variant.id, quantity=qty))
            total = to_money(total)
            create_sale(
                branch=branch,
                cashier=cashier,
                cart=cart,
                payments=self._payment_plan(spec.payment, total),
                client_sale_id=client_sale_id,
                customer=customer if spec.customer else None,
                completed_at=now - timedelta(days=spec.days_ago),
                business_date=today - timedelta(days=spec.days_ago),
            )

    # -- discount draft (pending, not finalised) ------------- #

    def _discount_draft(self, branch, cashier):
        from apps.catalog.models import ProductVariant
        from apps.sales.models import ApprovalStatus, ApprovalType
        from apps.sales.services.discounts import create_draft_sale, request_discount
        from apps.sales.services.sales import CartLine

        client_sale_id = seed_uuid("draft:pending-discount")
        variant = ProductVariant.objects.get(sku="BER-LUX-WHT-4L-S")
        draft = create_draft_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=variant.id, quantity=3)],
            client_sale_id=client_sale_id,
        )
        already = draft.approval_requests.filter(
            request_type=ApprovalType.DISCOUNT, status=ApprovalStatus.PENDING
        ).exists()
        if not already:
            request_discount(
                sale=draft,
                requested_by=cashier,
                amount=Decimal("2000.00"),
                reason="Bulk purchase goodwill discount for a regular trade customer",
            )

    # -- returns & refunds --------------------------------- #

    def _returns(self, branch, owner, cashier):
        from apps.sales.models import (
            ApprovalRequest,
            ApprovalStatus,
            ApprovalType,
            Sale,
        )
        from apps.sales.services.returns import (
            ApprovedLine,
            RefundLine,
            RequestLine,
            approve_return,
            reject_return,
            submit_return_request,
        )

        def get_or_submit(sale_key, client_return_id, reason, sku, qty):
            sale = Sale.objects.get(
                branch=branch, client_sale_id=seed_uuid(f"sale:{sale_key}")
            )
            existing = ApprovalRequest.objects.filter(
                sale=sale,
                request_type=ApprovalType.RETURN,
                requested_changes__client_return_id=str(client_return_id),
            ).first()
            if existing is not None:
                return sale, existing
            item = sale.items.get(variant__sku=sku)
            approval = submit_return_request(
                sale=sale,
                requested_by=cashier,
                reason=reason,
                lines=[RequestLine(sale_item_id=item.id, quantity=qty)],
                client_return_id=client_return_id,
            )
            return sale, approval

        # 1. approved resellable return with a refund + stock restoration
        crid_resellable = seed_uuid("return:resellable")
        sale, approval = get_or_submit(
            "return-source",
            crid_resellable,
            "Customer returned two unopened tins — wrong shade was ordered",
            "DUL-INT-MAG-4L-M",
            2,
        )
        if approval.status == ApprovalStatus.PENDING:
            item = sale.items.get(variant__sku="DUL-INT-MAG-4L-M")
            approve_return(
                approval=approval,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id, quantity=2, condition="RESELLABLE"
                    )
                ],
                refunds=[
                    RefundLine(
                        method="TRANSFER",
                        amount=item.line_total,
                        reference="RFND-DEMO-0001",
                    )
                ],
                client_return_id=crid_resellable,
                reviewer_note="Verified unopened and back on the shelf",
            )

        # 2. a pending return request awaiting the owner
        get_or_submit(
            "cash-change",
            seed_uuid("return:pending"),
            "Customer reports one tin may be faulty — awaiting inspection",
            "FIN-THIN-1L",
            1,
        )

        # 3. a rejected (opened / used) return
        _sale_r, approval_r = get_or_submit(
            "split-pos",
            seed_uuid("return:rejected"),
            "Customer wants to return an opened, part-used tub",
            "BER-LUX-WHT-20L-S",
            1,
        )
        if approval_r.status == ApprovalStatus.PENDING:
            reject_return(
                approval=approval_r,
                owner=owner,
                reviewer_note="Opened and partly used; not eligible under policy",
            )

    # -- expenses ---------------------------------------- #

    def _expenses(self, branch, owner):
        from apps.finance.models import Expense, ExpenseCategory
        from apps.finance.services.expenses import void_expense

        categories = {
            name: self._get_or_create_named(ExpenseCategory, branch, name)
            for name in ("Utilities", "Transport", "Rent", "Maintenance")
        }
        today = timezone.localdate()
        for cat_name, description, amount, days_ago in EXPENSES:
            Expense.objects.get_or_create(
                branch=branch,
                description=description,
                defaults={
                    "category": categories[cat_name],
                    "amount": Decimal(amount),
                    "expense_date": today - timedelta(days=days_ago),
                    "created_by": owner,
                },
            )

        cat_name, description, amount, days_ago = VOIDED_EXPENSE
        expense, _ = Expense.objects.get_or_create(
            branch=branch,
            description=description,
            defaults={
                "category": categories[cat_name],
                "amount": Decimal(amount),
                "expense_date": today - timedelta(days=days_ago),
                "created_by": owner,
            },
        )
        if not expense.is_voided:
            void_expense(
                expense=expense,
                actor=owner,
                reason="Duplicate of an existing fuel claim; removed from the books",
            )

    # -- stock counts & adjustment ---------------------- #

    def _stock_counts_and_adjustment(self, branch, owner):
        from apps.catalog.models import ProductVariant
        from apps.inventory.models import (
            InventoryBalance,
            StockCount,
            StockCountItem,
            StockCountStatus,
        )
        from apps.inventory.services.adjustments import adjust_stock
        from apps.inventory.services.stock_count import (
            apply_stock_count,
            submit_stock_count,
        )

        def balance_qty(sku):
            return InventoryBalance.objects.values_list("quantity", flat=True).get(
                branch=branch, variant__sku=sku
            )

        def count_items(stock_count, rows):
            for sku, counted in rows:
                StockCountItem.objects.get_or_create(
                    stock_count=stock_count,
                    variant=ProductVariant.objects.get(sku=sku),
                    defaults={
                        "system_quantity_snapshot": balance_qty(sku),
                        "counted_quantity": counted,
                    },
                )

        # A submitted count awaiting the owner (no variance).
        submitted, _ = StockCount.objects.get_or_create(
            branch=branch,
            reason="DEMO cycle count of aisle 1 — interior emulsions",
            defaults={"created_by": owner},
        )
        if not submitted.items.exists():
            count_items(
                submitted,
                [
                    ("DUL-WEA-GRY-20L-M", balance_qty("DUL-WEA-GRY-20L-M")),
                    ("FIN-EMU-WHT-20L-M", balance_qty("FIN-EMU-WHT-20L-M")),
                ],
            )
        if submitted.status == StockCountStatus.DRAFT:
            submit_stock_count(stock_count=submitted, actor=owner)

        # A safely applied count (a small negative variance, never below zero).
        applied, _ = StockCount.objects.get_or_create(
            branch=branch,
            reason="DEMO reconciliation of aisle 2 primers after the audit",
            defaults={"created_by": owner},
        )
        if not applied.items.exists():
            system = balance_qty("BER-ALK-WHT-20L")
            count_items(applied, [("BER-ALK-WHT-20L", max(system - 1, 0))])
        if applied.status == StockCountStatus.DRAFT:
            submit_stock_count(stock_count=applied, actor=owner)
        if applied.status != StockCountStatus.APPLIED:
            apply_stock_count(stock_count=applied, applied_by=owner)

        # One owner adjustment with a clear, detailed reason.
        adjust_stock(
            branch=branch,
            variant=ProductVariant.objects.get(sku="MEY-ROX-RED-4L"),
            direction="INCREASE",
            quantity=5,
            reason="Found an extra carton in the back storeroom during the audit",
            actor=owner,
            client_adjustment_id=seed_uuid("adjustment:storeroom-recount"),
        )

    # -- offline device (no active authorization) ------ #

    def _offline_device(self, branch, owner):
        from apps.accounts.models import DeviceStatus, RegisteredDevice

        RegisteredDevice.objects.get_or_create(
            branch=branch,
            name="Demo Till Tablet",
            defaults={"registered_by": owner, "status": DeviceStatus.ACTIVE},
        )
        # Deliberately no OfflineDeviceAuthorization: an ACTIVE one would block
        # ordinary online sales and stock actions for the branch.

    # -- DEMO-only teardown (correct dependency order) - #

    def _wipe(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from apps.accounts.models import (
            Branch,
            OfflineDeviceAuthorization,
            RegisteredDevice,
            User,
        )
        from apps.catalog.models import (
            Brand,
            Category,
            PriceHistory,
            Product,
            ProductVariant,
        )
        from apps.core.models import AuditLog
        from apps.finance.models import Expense, ExpenseCategory
        from apps.inventory.models import (
            InventoryBalance,
            Restock,
            StockCount,
            StockMovement,
            Supplier,
        )
        from apps.notifications.models import Notification, PushSubscription
        from apps.sales.models import (
            ApprovalRequest,
            Customer,
            OfflineSaleSyncRecord,
            ReceiptSequence,
            Sale,
            SaleReturn,
        )

        branch = Branch.objects.filter(code=DEMO_BRANCH_CODE).first()
        if branch is None:
            return
        user_ids = list(User.objects.filter(branch=branch).values_list("id", flat=True))

        # 1. seeded image files — only the ones we generated
        for product in Product.objects.filter(branch=branch).exclude(image=""):
            name = product.image.name
            if name and SEED_IMAGE_MARKER in posixpath.basename(name):
                product.image.delete(save=False)

        # 2. notifications + push subscriptions
        Notification.objects.filter(branch=branch).delete()
        PushSubscription.objects.filter(user_id__in=user_ids).delete()

        # 3. offline ledger -> authorization -> device
        OfflineSaleSyncRecord.objects.filter(branch=branch).delete()
        OfflineDeviceAuthorization.objects.filter(branch=branch).delete()
        RegisteredDevice.objects.filter(branch=branch).delete()

        # 4. returns (cascade Refund + SaleReturnItem) before approvals & sales
        SaleReturn.objects.filter(branch=branch).delete()

        # 5. approvals (PROTECT sale / stock_count / variant)
        ApprovalRequest.objects.filter(branch=branch).delete()

        # 6. sales (cascade Payment + SaleItem), customers, receipt sequences
        Sale.objects.filter(branch=branch).delete()
        Customer.objects.filter(branch=branch).delete()
        ReceiptSequence.objects.filter(branch=branch).delete()

        # 7. inventory: counts, restocks, suppliers, ledger, balances
        StockCount.objects.filter(branch=branch).delete()  # cascade items
        Restock.objects.filter(branch=branch).delete()  # cascade items
        Supplier.objects.filter(branch=branch).delete()
        StockMovement.objects.filter(branch=branch).delete()
        InventoryBalance.objects.filter(branch=branch).delete()

        # 8. finance
        Expense.objects.filter(branch=branch).delete()
        ExpenseCategory.objects.filter(branch=branch).delete()

        # 9. catalogue: prices -> variants -> products -> categories/brands
        PriceHistory.objects.filter(variant__product__branch=branch).delete()
        ProductVariant.objects.filter(product__branch=branch).delete()
        Product.objects.filter(branch=branch).delete()
        Category.objects.filter(branch=branch).delete()
        Brand.objects.filter(branch=branch).delete()

        # 10. audit rows scoped to the demo branch or its users
        AuditLog.objects.filter(Q(branch=branch) | Q(actor_id__in=user_ids)).delete()

        # 11. users (RecoveryCode + TOTPDevice cascade) then the branch itself
        TOTPDevice.objects.filter(user_id__in=user_ids).delete()
        User.objects.filter(id__in=user_ids).delete()
        Branch.objects.filter(code=DEMO_BRANCH_CODE).delete()

    def _rollback_written_images(self):
        for name, storage in getattr(self, "_written_images", []):
            try:
                if storage.exists(name):
                    storage.delete(name)
            except Exception:  # pragma: no cover - best effort cleanup
                pass

    # -- summary (no secrets) -------------------------- #

    def _collect_summary(self, branch):
        from apps.accounts.models import RegisteredDevice
        from apps.catalog.models import Brand, Category, Product, ProductVariant
        from apps.finance.models import Expense
        from apps.inventory.models import (
            InventoryBalance,
            Restock,
            RestockStatus,
            StockCount,
            StockCountStatus,
            StockMovement,
        )
        from apps.notifications.models import Notification
        from apps.notifications.services.stock_alerts import classify_stock
        from apps.sales.models import (
            ApprovalStatus,
            ApprovalType,
            Payment,
            Refund,
            Sale,
            SaleReturn,
            SaleStatus,
        )

        balances = InventoryBalance.objects.select_related("variant").filter(
            branch=branch
        )
        levels = [
            classify_stock(b.quantity, b.variant.low_stock_level) for b in balances
        ]
        approvals = branch.approval_requests
        self._summary = {
            "categories": Category.objects.filter(branch=branch).count(),
            "brands": Brand.objects.filter(branch=branch).count(),
            "products": Product.objects.filter(branch=branch).count(),
            "products_inactive": Product.objects.filter(
                branch=branch, is_active=False
            ).count(),
            "variants": ProductVariant.objects.filter(product__branch=branch).count(),
            "variants_inactive": ProductVariant.objects.filter(
                product__branch=branch, is_active=False
            ).count(),
            "images": Product.objects.filter(branch=branch).exclude(image="").count(),
            "suppliers": branch.suppliers.count(),
            "suppliers_inactive": branch.suppliers.filter(is_active=False).count(),
            "restocks_confirmed": Restock.objects.filter(
                branch=branch, status=RestockStatus.CONFIRMED
            ).count(),
            "restocks_draft": Restock.objects.filter(
                branch=branch, status=RestockStatus.DRAFT
            ).count(),
            "sales_completed": Sale.objects.filter(
                branch=branch, status=SaleStatus.COMPLETED
            ).count(),
            "sales_walkin": Sale.objects.filter(
                branch=branch, status=SaleStatus.COMPLETED, customer__isnull=True
            ).count(),
            "payments": Payment.objects.filter(sale__branch=branch).count(),
            "approvals_pending": approvals.filter(
                status=ApprovalStatus.PENDING
            ).count(),
            "approvals_decided": approvals.exclude(
                status=ApprovalStatus.PENDING
            ).count(),
            "returns": SaleReturn.objects.filter(branch=branch).count(),
            "refunds": Refund.objects.filter(sale_return__branch=branch).count(),
            "expenses": Expense.objects.filter(branch=branch).count(),
            "expenses_voided": Expense.objects.filter(
                branch=branch, is_voided=True
            ).count(),
            "stock_counts_submitted": StockCount.objects.filter(
                branch=branch, status=StockCountStatus.SUBMITTED
            ).count(),
            "stock_counts_applied": StockCount.objects.filter(
                branch=branch, status=StockCountStatus.APPLIED
            ).count(),
            "adjustments": StockMovement.objects.filter(
                branch=branch, reference_type="adjustment"
            ).count(),
            "stock_ok": levels.count("OK"),
            "stock_low": levels.count("LOW"),
            "stock_out": levels.count("OUT"),
            "notifications": Notification.objects.filter(branch=branch).count(),
            "devices": RegisteredDevice.objects.filter(branch=branch).count(),
            "discount_pending": approvals.filter(
                request_type=ApprovalType.DISCOUNT, status=ApprovalStatus.PENDING
            ).count(),
        }

    def _print_summary(self):
        s = self._summary
        write = self.stdout.write
        write(self.style.SUCCESS("Demo data ready."))
        write(f"Demo branch: {DEMO_BRANCH_CODE}")
        write("Users preserved: demo-owner, demo-cashier")
        write(f"Categories: {s['categories']}")
        write(f"Brands: {s['brands']}")
        write(
            f"Products: {s['products']} "
            f"({s['products'] - s['products_inactive']} active, "
            f"{s['products_inactive']} inactive)"
        )
        write(f"Variants: {s['variants']} ({s['variants_inactive']} inactive)")
        write(f"Seeded product images: {s['images']}")
        write(f"Suppliers: {s['suppliers']} ({s['suppliers_inactive']} inactive)")
        write(
            f"Restocks: {s['restocks_confirmed']} confirmed, "
            f"{s['restocks_draft']} draft"
        )
        write(
            f"Completed sales: {s['sales_completed']} "
            f"(walk-in: {s['sales_walkin']}), payments: {s['payments']}"
        )
        write(
            f"Approvals: {s['approvals_pending']} pending "
            f"({s['discount_pending']} discount), {s['approvals_decided']} decided"
        )
        write(f"Returns: {s['returns']} with {s['refunds']} refund(s)")
        write(f"Expenses: {s['expenses']} ({s['expenses_voided']} voided)")
        write(
            f"Stock counts: {s['stock_counts_submitted']} submitted, "
            f"{s['stock_counts_applied']} applied; "
            f"owner adjustments: {s['adjustments']}"
        )
        write(
            f"Stock levels — normal: {s['stock_ok']}, low: {s['stock_low']}, "
            f"out of stock: {s['stock_out']}"
        )
        write(f"Notifications generated: {s['notifications']}")
        write(
            f"Registered demo devices: {s['devices']} "
            "(no active offline authorization, no push credentials)"
        )
