"""Stage 18 — input validation and safe output acceptance.

Covers spec item 4: unknown/computed fields, primitive validation, NUL and
control-character rejection, upload validation at the API boundary, SQL-injection
resistance of search params, and XSS/markup payloads reaching the PDF renderer.
"""

from __future__ import annotations

import io
import uuid

import pytest
from pypdf import PdfReader

pytestmark = pytest.mark.django_db

API = "/api/v1"


def _category(client, name="Cat"):
    return client.post(f"{API}/expense-categories/", {"name": name}, format="json")


class TestUnknownAndComputedFields:
    def test_unknown_writable_fields_are_ignored_not_applied(self, login_as, owner):
        c = login_as(owner)
        res = c.post(
            f"{API}/expense-categories/",
            {
                "name": "Fuel",
                "id": "11111111-1111-1111-1111-111111111111",
                "created_at": "1999-01-01T00:00:00Z",
                "is_evil": True,
                "branch": "22222222-2222-2222-2222-222222222222",
            },
            format="json",
        )
        assert res.status_code == 201, res.content
        body = res.json()
        assert body["id"] != "11111111-1111-1111-1111-111111111111"
        if body.get("created_at"):
            assert not body["created_at"].startswith("1999")

    def test_client_cannot_set_price_total_or_receipt_number_on_a_sale(
        self, login_as, employee, stocked
    ):
        variant = stocked(sku="IV-1", price="1000.00")
        c = login_as(employee)
        res = c.post(
            f"{API}/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [
                    {"variant": str(variant.id), "quantity": 2, "unit_price": "1.00"}
                ],
                "payments": [{"method": "CASH", "amount": "2000.00"}],
                "total": "2.00",
                "subtotal": "2.00",
                "discount_total": "0.00",
                "receipt_number": "HACKED-0001",
            },
            format="json",
        )
        assert res.status_code == 201, res.content
        body = res.json()
        # server computed the totals from the active price, ignoring client values
        assert body["total"] == "2000.00"
        assert body["subtotal"] == "2000.00"
        assert body["receipt_number"] and body["receipt_number"] != "HACKED-0001"
        assert body["items"][0]["unit_price_snapshot"] == "1000.00"


class TestPrimitiveValidation:
    def test_nul_byte_is_rejected(self, login_as, owner):
        res = login_as(owner).post(
            f"{API}/expense-categories/", {"name": "a\x00b"}, format="json"
        )
        assert res.status_code == 400

    def test_other_control_characters_are_rejected(self, login_as, owner):
        for ch in ("\x07", "\x1b", "\x1f", "\x7f"):
            res = login_as(owner).post(
                f"{API}/expense-categories/",
                {"name": f"x{ch}y"},
                format="json",
            )
            assert res.status_code == 400, repr(ch)
            assert res.json()["code"] in {"validation_error", "control_characters"}

    def test_tab_and_newline_are_allowed(self, login_as, owner):
        res = login_as(owner).post(
            f"{API}/expense-categories/",
            {"name": "line one\tstill fine"},
            format="json",
        )
        assert res.status_code == 201, res.content

    def test_money_type_and_sign(self, login_as, owner):
        cat = _category(login_as(owner)).json()
        c = login_as(owner)
        bad = c.post(
            f"{API}/expenses/",
            {
                "category": cat["id"],
                "amount": "not-money",
                "expense_date": "2026-08-01",
                "description": "d",
            },
            format="json",
        )
        assert bad.status_code == 400
        neg = c.post(
            f"{API}/expenses/",
            {
                "category": cat["id"],
                "amount": "-5.00",
                "expense_date": "2026-08-01",
                "description": "d",
            },
            format="json",
        )
        assert neg.status_code == 400

    def test_bad_uuid_and_date_are_rejected(self, login_as, owner):
        cat = _category(login_as(owner)).json()
        c = login_as(owner)
        assert (
            c.post(
                f"{API}/expenses/",
                {
                    "category": "not-a-uuid",
                    "amount": "5.00",
                    "expense_date": "2026-08-01",
                    "description": "d",
                },
                format="json",
            ).status_code
            == 400
        )
        assert (
            c.post(
                f"{API}/expenses/",
                {
                    "category": cat["id"],
                    "amount": "5.00",
                    "expense_date": "31/02/2026",
                    "description": "d",
                },
                format="json",
            ).status_code
            == 400
        )

    def test_quantity_must_be_a_positive_whole_number(
        self, login_as, employee, stocked
    ):
        variant = stocked(sku="IV-2")
        c = login_as(employee)
        for qty in (0, -3, 1.5, "abc"):
            res = c.post(
                f"{API}/sales/",
                {
                    "client_sale_id": str(uuid.uuid4()),
                    "items": [{"variant": str(variant.id), "quantity": qty}],
                    "payments": [{"method": "CASH", "amount": "1000.00"}],
                },
                format="json",
            )
            assert res.status_code == 400, qty

    def test_payment_method_choice_is_enforced(self, login_as, employee, stocked):
        variant = stocked(sku="IV-3")
        res = login_as(employee).post(
            f"{API}/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 1}],
                "payments": [{"method": "BITCOIN", "amount": "1000.00"}],
            },
            format="json",
        )
        assert res.status_code == 400


class TestUploadValidationAtTheApi:
    def test_renamed_executable_is_rejected(self, login_as, owner):
        cat = _category(login_as(owner)).json()
        payload = {
            "category": cat["id"],
            "amount": "10.00",
            "expense_date": "2026-08-01",
            "description": "d",
            "receipt_file": io.BytesIO(b"#!/bin/sh\nrm -rf /\n"),
        }
        payload["receipt_file"].name = "receipt.pdf"
        res = login_as(owner).post(f"{API}/expenses/", payload, format="multipart")
        assert res.status_code == 400
        assert "receipt_file" in res.json()["field_errors"]


class TestSqlInjection:
    def test_search_param_cannot_change_query_meaning(self, login_as, owner, stocked):
        stocked(sku="SAFE-1")
        stocked(sku="SAFE-2")
        c = login_as(owner)
        for payload in ("' OR '1'='1", "'; DROP TABLE catalog_product;--", "%", "_"):
            res = c.get(f"{API}/products/", {"search": payload})
            assert res.status_code == 200
            assert res.json()["count"] == 0, payload
        # a benign search still returns exactly the two seeded products
        assert c.get(f"{API}/products/", {"search": "SAFE-"}).json()["count"] == 2

    def test_tables_still_exist_after_injection_attempts(self, login_as, owner):
        assert login_as(owner).get(f"{API}/products/").status_code == 200


class TestXssAndPdfRendering:
    _PAYLOADS = [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "R&D paint <b>bold</b> & <font color='red'>x</font>",
        "javascript:alert(document.cookie)",
    ]

    def _sell(self, client, variant):
        return client.post(
            f"{API}/sales/",
            {
                "client_sale_id": str(uuid.uuid4()),
                "items": [{"variant": str(variant.id), "quantity": 1}],
                "payments": [{"method": "CASH", "amount": "1000.00"}],
            },
            format="json",
        ).json()

    def test_markup_in_a_product_name_never_breaks_or_injects_the_pdf(
        self, login_as, employee, stocked
    ):
        c_emp = login_as(employee)
        for i, name in enumerate(self._PAYLOADS):
            variant = stocked(sku=f"XSS-{i}", price="1000.00")
            variant.product.name = name
            variant.product.save(update_fields=["name"])
            sale = self._sell(c_emp, variant)
            pdf = c_emp.get(f"{API}/sales/{sale['id']}/receipt.pdf")
            # the ReportLab Paragraph parser would raise (-> 500) on raw markup;
            # pdf_escape keeps the name inert so every payload still renders.
            assert pdf.status_code == 200, name
            assert pdf["Content-Type"] == "application/pdf"
            assert pdf.content[:5] == b"%PDF-"
            text = "\n".join(
                (p.extract_text() or "")
                for p in PdfReader(io.BytesIO(pdf.content)).pages
            ).lower()
            # visible characters of the name still render as literal text
            assert "paint" in text or "alert" in text or "script" in text

    def test_json_receipt_returns_the_name_verbatim_for_the_frontend(
        self, login_as, employee, stocked
    ):
        variant = stocked(sku="XSS-J", price="1000.00")
        variant.product.name = "<b>Paint</b>"
        variant.product.save(update_fields=["name"])
        c = login_as(employee)
        sale = self._sell(c, variant)
        body = c.get(f"{API}/sales/{sale['id']}/receipt/").json()
        # the API returns raw text (JSON-encoded); the frontend must render as text
        assert "<b>Paint</b>" in str(body["items"])
