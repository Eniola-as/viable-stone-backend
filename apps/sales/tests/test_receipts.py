"""Stage 12 — A4 PDF receipt: generation, authz, branch isolation, snapshots."""

import io
import uuid
from decimal import Decimal

import pytest
from pypdf import PdfReader

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.catalog.services.pricing import set_active_price
from apps.sales.models import Sale
from apps.sales.services.receipts import receipt_context, render_receipt_pdf
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

from .factories import stocked_variant

pytestmark = pytest.mark.django_db

RECEIPT_PDF = "/api/v1/sales/{}/receipt.pdf"
RECEIPT_JSON = "/api/v1/sales/{}/receipt/"

FORBIDDEN_SUBSTRINGS = [b"unit_cost", b"average_unit_cost", b"cost", b"profit", b"COGS"]


def _pages_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [page.extract_text() or "" for page in reader.pages]


def _sale(
    branch, cashier, owner, *, qty=2, price="1500.00", cost="900.00", payments=None
):
    variant = stocked_variant(
        branch, owner, price=price, quantity=qty + 5, unit_cost=cost
    )
    return (
        create_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=variant.id, quantity=qty)],
            payments=payments
            or [
                PaymentLine(
                    method="CASH",
                    amount=Decimal(price) * qty,
                    tendered_amount=Decimal(price) * qty,
                )
            ],
            client_sale_id=uuid.uuid4(),
        ),
        variant,
    )


class TestReceiptContext:
    def test_context_uses_snapshots_and_hides_cost(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _variant = _sale(branch, cashier, owner, qty=2, price="1500.00")
        ctx = receipt_context(sale)

        assert ctx["receipt_number"] == sale.receipt_number
        assert ctx["status"] == "COMPLETED"
        assert ctx["status_label"] == "COMPLETED / PAID"
        assert ctx["total"] == "3,000.00"
        assert ctx["subtotal"] == "3,000.00"
        assert ctx["items"][0]["quantity"] == 2
        assert ctx["items"][0]["unit_price"] == "1,500.00"
        assert ctx["items"][0]["line_total"] == "3,000.00"
        assert ctx["branch"]["code"] == branch.code
        assert ctx["cashier"]
        # Africa/Lagos is UTC+1, no DST
        assert ctx["issued_at_iso"].endswith("+01:00")
        # no cost / id / audit keys anywhere
        flat = str(ctx).lower()
        assert "cost" not in flat and "profit" not in flat
        assert str(sale.id) not in str(ctx)
        assert str(sale.items.first().id) not in str(ctx)

    def test_context_business_identity_from_settings(self, branch, owner, settings):
        settings.BUSINESS_IDENTITY = {
            "name": "Viable Stone Paints & Coatings Enterprise",
            "phone": "+234 800 000 0000",
            "email": "shop@viablestone.example",
            "address": "12 Paint Road, Lagos",
            "logo_path": "",
            "currency_symbol": "NGN",
        }
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner)
        ctx = receipt_context(sale)
        assert ctx["business"]["name"] == "Viable Stone Paints & Coatings Enterprise"
        assert ctx["business"]["email"] == "shop@viablestone.example"
        assert ctx["business"]["address"] == "12 Paint Road, Lagos"

    def test_split_payment_breakdown(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(
            branch,
            cashier,
            owner,
            qty=3,
            price="1000.00",
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("2000.00"),
                    tendered_amount=Decimal("2500.00"),
                ),
                PaymentLine(
                    method="TRANSFER", amount=Decimal("1000.00"), reference="TRX-1"
                ),
            ],
        )
        ctx = receipt_context(sale)
        methods = {p["method"]: p for p in ctx["payments"]}
        assert methods["CASH"]["amount"] == "2,000.00"
        assert methods["CASH"]["tendered"] == "2,500.00"
        assert methods["CASH"]["change"] == "500.00"
        assert methods["TRANSFER"]["amount"] == "1,000.00"
        assert ctx["change_due"] == "500.00"

    def test_optional_customer_included_when_present(self, branch, owner):
        from apps.sales.services.customers import resolve_customer

        cashier = EmployeeFactory(branch=branch)
        customer = resolve_customer(branch, {"name": "Ada", "phone": "08031112222"})
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        sale = create_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=variant.id, quantity=1)],
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("1000.00"),
                    tendered_amount=Decimal("1000.00"),
                )
            ],
            client_sale_id=uuid.uuid4(),
            customer=customer,
        )
        ctx = receipt_context(sale)
        assert ctx["customer"]["name"] == "Ada"
        assert ctx["customer"]["phone"] == "08031112222"

    def test_walk_in_sale_has_no_customer_block(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner)
        assert receipt_context(sale)["customer"] is None


class TestReceiptPdf:
    def test_pdf_is_wellformed_and_nonempty(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner)
        pdf = render_receipt_pdf(sale)
        assert pdf[:5] == b"%PDF-"
        assert b"%%EOF" in pdf
        assert len(pdf) > 1200

    def test_pdf_text_has_identity_totals_status_no_cost(self, branch, owner):
        cashier = EmployeeFactory(branch=branch, username="tola")
        sale, _ = _sale(branch, cashier, owner, qty=2, price="1500.00", cost="912.34")
        pdf = render_receipt_pdf(sale, compress=False)
        assert b"Viable Stone Paints & Coatings Enterprise" in pdf
        assert sale.receipt_number.encode() in pdf
        assert b"3,000.00" in pdf  # total / line total
        assert b"1,500.00" in pdf  # unit price
        assert b"COMPLETED" in pdf and b"PAID" in pdf
        assert b"tola" in pdf  # cashier
        assert sale.branch.name.encode() in pdf
        # cost snapshot value and cost words must never appear
        assert b"912.34" not in pdf
        for token in FORBIDDEN_SUBSTRINGS:
            assert token not in pdf
        # internal database ids must never appear
        assert str(sale.id).encode() not in pdf
        assert str(sale.items.first().id).encode() not in pdf
        assert str(sale.payments.first().id).encode() not in pdf

    def test_pdf_embeds_logo_when_configured(self, branch, owner, tmp_path, settings):
        from PIL import Image as PILImage

        png = tmp_path / "logo.png"
        PILImage.new("RGB", (64, 64), (10, 80, 160)).save(png)
        settings.BUSINESS_IDENTITY = {
            **settings.BUSINESS_IDENTITY,
            "logo_path": str(png),
        }
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner)
        with_logo = render_receipt_pdf(sale, compress=False)
        assert with_logo[:5] == b"%PDF-"
        assert b"/Image" in with_logo or b"/XObject" in with_logo

    def test_pdf_shows_split_payment_and_change(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(
            branch,
            cashier,
            owner,
            qty=3,
            price="1000.00",
            payments=[
                PaymentLine(
                    method="CASH",
                    amount=Decimal("2000.00"),
                    tendered_amount=Decimal("2500.00"),
                ),
                PaymentLine(method="POS", amount=Decimal("1000.00"), reference="POS-7"),
            ],
        )
        pdf = render_receipt_pdf(sale, compress=False)
        assert b"Cash" in pdf
        assert b"POS" in pdf
        assert b"2,000.00" in pdf
        assert b"2,500.00" in pdf  # tendered
        assert b"500.00" in pdf  # change

    def test_pdf_stable_after_product_name_and_price_change(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, variant = _sale(branch, cashier, owner, qty=2, price="1500.00")
        before = render_receipt_pdf(sale, compress=False)
        assert variant.product.name.encode() in before

        old_name = variant.product.name
        variant.product.name = "TOTALLY DIFFERENT NAME"
        variant.product.save(update_fields=["name"])
        set_active_price(variant=variant, amount=Decimal("9999.99"), changed_by=owner)

        after = render_receipt_pdf(Sale.objects.get(pk=sale.pk), compress=False)
        assert old_name.encode() in after
        assert b"TOTALLY DIFFERENT NAME" not in after
        assert b"9,999.99" not in after
        assert b"1,500.00" in after
        assert b"3,000.00" in after


class TestReceiptApiAuthorization:
    def _make(self, branch, cashier, owner):
        sale, _ = _sale(branch, cashier, owner)
        return sale

    def test_pdf_endpoint_content_type_is_exactly_application_pdf(
        self, login_as, branch, owner
    ):
        cashier = EmployeeFactory(branch=branch)
        sale = self._make(branch, cashier, owner)
        res = login_as(cashier).get(RECEIPT_PDF.format(sale.id))
        assert res.status_code == 200
        # exact string — no charset, no extra parameters
        assert res.headers["Content-Type"] == "application/pdf"
        assert res["Content-Disposition"] == (
            f'inline; filename="{sale.receipt_number}.pdf"'
        )
        assert res["X-Content-Type-Options"] == "nosniff"
        assert res["Cache-Control"] == "private, no-store"
        assert int(res["Content-Length"]) == len(res.content)
        assert res.content[:5] == b"%PDF-"

    def test_json_receipt_endpoint(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale = self._make(branch, cashier, owner)
        res = login_as(cashier).get(RECEIPT_JSON.format(sale.id))
        assert res["Cache-Control"] == "private, no-store"
        body = res.json()
        assert body["receipt_number"] == sale.receipt_number
        assert body["status_label"] == "COMPLETED / PAID"
        assert "cost" not in str(body).lower()
        assert str(sale.id) not in str(body)

    def test_other_cashier_cannot_read_receipt(self, login_as, branch, owner):
        mine = EmployeeFactory(branch=branch, username="mine")
        other = EmployeeFactory(branch=branch, username="other")
        sale = self._make(branch, mine, owner)
        assert login_as(other).get(RECEIPT_PDF.format(sale.id)).status_code == 404
        assert login_as(other).get(RECEIPT_JSON.format(sale.id)).status_code == 404

    def test_owner_can_read_any_branch_receipt(self, login_as, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale = self._make(branch, cashier, owner)
        assert login_as(owner).get(RECEIPT_PDF.format(sale.id)).status_code == 200

    def test_cross_branch_receipt_is_404(self, login_as, branch, owner):
        other_branch = BranchFactory(code="VS62")
        other_owner = OwnerFactory(branch=other_branch)
        other_cashier = EmployeeFactory(branch=other_branch)
        outside = self._make(other_branch, other_cashier, other_owner)
        assert login_as(owner).get(RECEIPT_PDF.format(outside.id)).status_code == 404
        assert login_as(owner).get(RECEIPT_JSON.format(outside.id)).status_code == 404

    def test_receipt_requires_authentication(self, api_client, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale = self._make(branch, cashier, owner)
        assert api_client.get(RECEIPT_PDF.format(sale.id)).status_code == 401


class TestCurrencyRendering:
    def test_currency_token_renders_and_extracts_from_pdf(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner, qty=2, price="1500.00")
        text = "\n".join(_pages_text(render_receipt_pdf(sale)))
        # currency labelled once per money column in the items table...
        assert "Unit Price (NGN)" in text
        assert "Line Total (NGN)" in text
        assert "1,500.00" in text and "3,000.00" in text
        # ...and prefixed on every totals / payment amount, extractable intact
        assert "NGN 3,000.00" in text  # TOTAL (bold) extracts cleanly
        assert "Subtotal" in text
        # the built-in font has no Naira glyph; it must never leak into output
        assert "₦" not in text

    def test_non_latin_currency_symbol_falls_back_to_ngn(self, branch, owner, settings):
        settings.BUSINESS_IDENTITY = {
            **settings.BUSINESS_IDENTITY,
            "currency_symbol": "₦",  # ₦ — no glyph in the built-in font
        }
        cashier = EmployeeFactory(branch=branch)
        sale, _ = _sale(branch, cashier, owner, qty=1, price="1000.00")
        ctx = receipt_context(sale)
        assert ctx["currency"] == "NGN"
        text = "\n".join(_pages_text(render_receipt_pdf(sale)))
        assert "NGN 1,000.00" in text
        assert "₦" not in text


class TestMultiPageReceipt:
    def _big_sale(self, branch, owner, cashier, *, lines=34):
        long_desc = (
            "Premium weather-guard exterior emulsion, low-VOC, anti-fungal, "
            "silk finish, tint base D, for masonry and rendered surfaces"
        )
        items, payments_total = [], Decimal("0.00")
        for i in range(lines):
            variant = stocked_variant(
                branch,
                owner,
                price="1250.00",
                quantity=5,
                sku=f"BIG-{i:03d}",
            )
            variant.product.name = f"{long_desc} [row {i:02d}]"
            variant.product.save(update_fields=["name"])
            items.append(CartLine(variant_id=variant.id, quantity=2))
            payments_total += Decimal("2500.00")
        half = (payments_total / 2).quantize(Decimal("0.01"))
        return create_sale(
            branch=branch,
            cashier=cashier,
            cart=items,
            payments=[
                PaymentLine(
                    method="CASH", amount=half, tendered_amount=half + Decimal("500.00")
                ),
                PaymentLine(
                    method="TRANSFER",
                    amount=payments_total - half,
                    reference="TRX-MULTIPAGE",
                ),
            ],
            client_sale_id=uuid.uuid4(),
        )

    def test_multipage_pdf_repeats_header_and_never_clips(self, branch, owner):
        cashier = EmployeeFactory(branch=branch, username="pat")
        sale = self._big_sale(branch, owner, cashier, lines=34)
        pages = _pages_text(render_receipt_pdf(sale))

        assert len(pages) >= 2, "34 line items should span multiple A4 pages"
        # items-table header repeats on every page that carries item rows
        assert "Description" in pages[0] and "Qty" in pages[0]
        assert "Description" in pages[1]
        # page furniture present on every page
        for i, page in enumerate(pages, start=1):
            assert f"Page {i}" in page
        # business identity on page 1
        assert "Viable Stone Paints & Coatings Enterprise" in pages[0]
        # totals + status land on the final page, intact
        last = pages[-1]
        assert "TOTAL" in last
        assert "COMPLETED / PAID" in last
        assert "TRX-MULTIPAGE" in last
        # every long description is present in full somewhere (nothing truncated)
        whole = "".join(p.replace("\n", " ") for p in pages)
        for item in receipt_context(sale)["items"]:
            words = item["description"].split()
            assert words[0] in whole and words[-1].strip("]") in whole


def test_env_example_documents_every_business_identity_var(settings):
    """.env.example must list every BUSINESS_* var read by BUSINESS_IDENTITY."""

    from pathlib import Path

    env_example = (Path(settings.BASE_DIR) / ".env.example").read_text(encoding="utf-8")
    for var in (
        "BUSINESS_NAME",
        "BUSINESS_PHONE",
        "BUSINESS_EMAIL",
        "BUSINESS_ADDRESS",
        "BUSINESS_LOGO_PATH",
        "BUSINESS_CURRENCY_SYMBOL",
    ):
        assert var in env_example, f"{var} missing from .env.example"
    # no obvious secret material committed
    lowered = env_example.lower()
    for token in ("secret_key=django-insecure", "password=", "aws_secret", "api_key="):
        assert token not in lowered
