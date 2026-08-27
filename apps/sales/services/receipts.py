"""A4 paid-receipt PDF, built entirely from the immutable sale snapshots.

The PDF and the JSON receipt never expose cost prices, profit, internal database
ids or audit data. Business identity/contact text comes from
``settings.BUSINESS_IDENTITY`` — never hard-coded here.

Currency is shown as the ASCII code ``NGN``. The built-in PDF fonts have no
Naira glyph (U+20A6); a configured non-Latin-1 symbol is downgraded to ``NGN``
so the amount is always legible and text-extractable.
"""

from __future__ import annotations

import io
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("apps.sales.receipts")

_METHOD_LABELS = {"CASH": "Cash", "TRANSFER": "Transfer", "POS": "POS"}


def _money(value) -> str:
    return f"{Decimal(value):,.2f}"


def _safe_currency(symbol: str) -> str:
    symbol = (symbol or "NGN").strip()
    try:
        symbol.encode("latin-1")  # renderable by the built-in PDF fonts
    except UnicodeEncodeError:
        logger.warning(
            "BUSINESS_CURRENCY_SYMBOL %r has no glyph in the receipt font; "
            "using 'NGN'.",
            symbol,
        )
        return "NGN"
    return symbol


def _cashier_label(sale) -> str:
    name = (sale.cashier.get_full_name() or "").strip()
    username = sale.cashier.username
    return f"{name} ({username})" if name else username


def _identity() -> dict:
    base = {
        "name": "",
        "phone": "",
        "email": "",
        "address": "",
        "logo_path": "",
        "currency_symbol": "NGN",
    }
    base.update(getattr(settings, "BUSINESS_IDENTITY", {}) or {})
    base["currency_symbol"] = _safe_currency(base["currency_symbol"])
    return base


def receipt_context(sale) -> dict:
    """Plain, snapshot-only data shared by the JSON receipt and the PDF."""

    identity = _identity()
    issued = timezone.localtime(sale.completed_at or sale.created_at)

    items = [
        {
            "description": (
                f"{item.product_name_snapshot} — {item.variant_description_snapshot}"
                if item.variant_description_snapshot
                else item.product_name_snapshot
            ),
            "sku": item.sku_snapshot,
            "quantity": item.quantity,
            "unit_price": _money(item.unit_price_snapshot),
            "line_total": _money(item.line_total),
        }
        for item in sale.items.all()
    ]

    payments = []
    for pay in sale.payments.all():
        row = {
            "method": pay.method,
            "label": _METHOD_LABELS.get(pay.method, pay.method),
            "amount": _money(pay.amount),
            "reference": pay.reference or "",
        }
        if pay.method == "CASH" and pay.tendered_amount is not None:
            row["tendered"] = _money(pay.tendered_amount)
            row["change"] = _money(pay.tendered_amount - pay.amount)
        payments.append(row)

    customer = None
    if sale.customer and (sale.customer.name or sale.customer.phone):
        customer = {
            "name": sale.customer.name or "",
            "phone": sale.customer.phone or "",
        }

    return {
        "business": {
            "name": identity["name"],
            "phone": identity["phone"],
            "email": identity["email"],
            "address": identity["address"],
        },
        "currency": identity["currency_symbol"],
        "receipt_number": sale.receipt_number,
        "issued_at": issued.strftime("%d %b %Y, %I:%M %p"),
        "issued_at_iso": issued.isoformat(),
        "branch": {"name": sale.branch.name, "code": sale.branch.code},
        "cashier": _cashier_label(sale),
        "customer": customer,
        "items": items,
        "subtotal": _money(sale.subtotal),
        "discount_total": _money(sale.discount_total),
        "total": _money(sale.total),
        "payments": payments,
        "cash_tendered": next(
            (p["tendered"] for p in payments if "tendered" in p), None
        ),
        "change_due": _money(sale.change_due),
        "status": sale.status,
        "status_label": "COMPLETED / PAID",
    }


def _resolve_logo_path(raw: str) -> str | None:
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(settings.BASE_DIR, raw)
    return path if os.path.isfile(path) else None


def _page_furniture(canvas, doc):
    """Thin footer rule + 'Page X of Y' on every page."""

    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
    y = 12 * mm
    canvas.line(doc.leftMargin, y, doc.leftMargin + doc.width, y)
    canvas.drawRightString(doc.leftMargin + doc.width, y - 4 * mm, f"Page {doc.page}")
    canvas.restoreState()


def render_receipt_pdf(sale, *, compress: bool = True) -> bytes:
    ctx = receipt_context(sale)
    cur = ctx["currency"]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"Receipt {ctx['receipt_number']}",
        author=ctx["business"]["name"],
    )
    doc.pageCompression = 1 if compress else 0

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    small = ParagraphStyle("small", parent=normal, fontSize=8, leading=10)
    name_style = ParagraphStyle(
        "biz", parent=styles["Title"], fontSize=16, leading=19, alignment=0
    )
    # wordWrap="CJK" breaks even space-less strings, so no cell can overflow.
    cell = ParagraphStyle("cell", parent=normal, fontSize=9, leading=11, wordWrap="CJK")
    num = ParagraphStyle("num", parent=cell, alignment=2)
    label = ParagraphStyle("label", parent=normal, fontSize=9, leading=11)
    label_r = ParagraphStyle("label_r", parent=label, alignment=2)

    story: list = []

    # --- Header: logo + identity ------------------------------------- #
    identity_block = [Paragraph(ctx["business"]["name"], name_style)]
    for key in ("address", "phone", "email"):
        if ctx["business"][key]:
            identity_block.append(Paragraph(ctx["business"][key], small))

    logo_path = _resolve_logo_path(_identity()["logo_path"])
    if logo_path:
        try:
            logo = Image(logo_path, width=26 * mm, height=26 * mm, kind="proportional")
            header = Table(
                [[logo, identity_block]], colWidths=[30 * mm, doc.width - 30 * mm]
            )
        except Exception:  # pragma: no cover - malformed image file
            header = Table([[identity_block]], colWidths=[doc.width])
    else:
        header = Table([[identity_block]], colWidths=[doc.width])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 7 * mm))
    story.append(Paragraph("SALES RECEIPT", styles["Heading2"]))
    story.append(Spacer(1, 2 * mm))

    # --- Meta block ------------------------------------------------- #
    meta_rows = [
        [
            Paragraph(f"<b>Receipt No:</b> {ctx['receipt_number']}", label),
            Paragraph(f"<b>Date:</b> {ctx['issued_at']}", label),
        ],
        [
            Paragraph(
                f"<b>Branch:</b> {ctx['branch']['name']} ({ctx['branch']['code']})",
                label,
            ),
            Paragraph(f"<b>Cashier:</b> {ctx['cashier']}", label),
        ],
    ]
    if ctx["customer"]:
        meta_rows.append(
            [
                Paragraph(f"<b>Customer:</b> {ctx['customer']['name'] or '-'}", label),
                Paragraph(f"<b>Phone:</b> {ctx['customer']['phone'] or '-'}", label),
            ]
        )
    meta = Table(meta_rows, colWidths=[doc.width / 2] * 2)
    meta.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(meta)
    story.append(Spacer(1, 5 * mm))

    # --- Items table (header repeats on every page) -------------- #
    qty_w, price_w, total_w = 15 * mm, 32 * mm, 32 * mm
    desc_w = doc.width - qty_w - price_w - total_w
    rows = [
        [
            Paragraph("<b>Description</b>", cell),
            Paragraph("<b>Qty</b>", num),
            Paragraph(f"<b>Unit Price ({cur})</b>", num),
            Paragraph(f"<b>Line Total ({cur})</b>", num),
        ]
    ]
    for item in ctx["items"]:
        rows.append(
            [
                Paragraph(item["description"], cell),
                Paragraph(str(item["quantity"]), num),
                Paragraph(item["unit_price"], num),
                Paragraph(item["line_total"], num),
            ]
        )
    items_table = Table(rows, colWidths=[desc_w, qty_w, price_w, total_w], repeatRows=1)
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    # --- Totals ------------------------------------------------- #
    total_rows = [
        [
            Paragraph("Subtotal", label_r),
            Paragraph(f"{cur} {ctx['subtotal']}", num),
        ]
    ]
    if Decimal(ctx["discount_total"].replace(",", "")) > 0:
        total_rows.append(
            [
                Paragraph("Discount", label_r),
                Paragraph(f"- {cur} {ctx['discount_total']}", num),
            ]
        )
    total_rows.append(
        [
            Paragraph("<b>TOTAL</b>", label_r),
            Paragraph(f"<b>{cur} {ctx['total']}</b>", num),
        ]
    )
    totals = Table(total_rows, colWidths=[doc.width - 62 * mm, 62 * mm])
    totals.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(totals)
    story.append(Spacer(1, 5 * mm))

    # --- Payments --------------------------------------------- #
    story.append(Paragraph("Payment", styles["Heading3"]))
    pay_rows = []
    for pay in ctx["payments"]:
        text = pay["label"]
        if pay.get("reference"):
            text = f"{text} (ref: {pay['reference']})"
        pay_rows.append(
            [Paragraph(text, label), Paragraph(f"{cur} {pay['amount']}", num)]
        )
        if "tendered" in pay:
            pay_rows.append(
                [
                    Paragraph("Cash tendered", label),
                    Paragraph(f"{cur} {pay['tendered']}", num),
                ]
            )
            pay_rows.append(
                [
                    Paragraph("Change", label),
                    Paragraph(f"{cur} {pay['change']}", num),
                ]
            )
    pay_table = Table(pay_rows, colWidths=[doc.width - 62 * mm, 62 * mm])
    pay_table.setStyle(
        TableStyle(
            [
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(pay_table)
    story.append(Spacer(1, 6 * mm))

    status = Table(
        [[Paragraph("<b>STATUS: COMPLETED / PAID</b>", normal)]], colWidths=[doc.width]
    )
    status.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#15803d")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#dcfce7")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#14532d")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(status)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Thank you for your patronage.", small))

    doc.build(story, onFirstPage=_page_furniture, onLaterPages=_page_furniture)
    return buffer.getvalue()
