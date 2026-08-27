"""Stage 17 — private file uploads validated by real content, not extension."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import serializers

from apps.core.validators import sniff_content_type, validate_private_upload

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_PDF = b"%PDF-1.7\n" + b"binary junk" * 8
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
_SCRIPT = b"#!/bin/sh\nrm -rf /\n"


def _file(data, name):
    return SimpleUploadedFile(name, data)


class TestSniff:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (_PNG, "image/png"),
            (_JPEG, "image/jpeg"),
            (_PDF, "application/pdf"),
            (_WEBP, "image/webp"),
            (_SCRIPT, None),
            (b"RIFF\x00\x00\x00\x00AVI ", None),  # RIFF but not WEBP
        ],
    )
    def test_sniff_content_type(self, data, expected):
        assert sniff_content_type(data) == expected


class TestValidatePrivateUpload:
    def test_accepts_a_real_png_named_png(self):
        validate_private_upload(_file(_PNG, "receipt.png"))

    def test_accepts_a_real_pdf_named_pdf(self):
        validate_private_upload(_file(_PDF, "receipt.pdf"))

    def test_rejects_pdf_bytes_wearing_a_png_extension(self):
        with pytest.raises(serializers.ValidationError):
            validate_private_upload(_file(_PDF, "sneaky.png"))

    def test_rejects_a_script_renamed_to_pdf(self):
        with pytest.raises(serializers.ValidationError):
            validate_private_upload(_file(_SCRIPT, "malware.pdf"))

    def test_rejects_oversized_file(self, settings):
        settings.MAX_UPLOAD_SIZE = 128
        with pytest.raises(serializers.ValidationError):
            validate_private_upload(_file(_PDF + b"x" * 500, "big.pdf"))

    def test_leaves_the_stream_position_untouched(self):
        f = _file(_PNG, "receipt.png")
        f.seek(3)
        validate_private_upload(f)
        assert f.tell() == 3


@pytest.mark.django_db
class TestExpenseReceiptUploadAPI:
    def _category(self, owner):
        from apps.finance.models import ExpenseCategory

        return ExpenseCategory.objects.create(branch=owner.branch, name="Fuel")

    def test_owner_cannot_upload_a_disguised_executable(self, login_as, owner):
        category = self._category(owner)
        payload = {
            "category": str(category.id),
            "amount": "1500.00",
            "expense_date": "2026-08-27",
            "description": "diesel",
            "receipt_file": io.BytesIO(_SCRIPT),
        }
        payload["receipt_file"].name = "receipt.pdf"
        res = login_as(owner).post("/api/v1/expenses/", payload, format="multipart")
        assert res.status_code == 400
        assert "receipt_file" in res.json()["field_errors"]

    def test_owner_can_upload_a_real_pdf_receipt(self, login_as, owner):
        category = self._category(owner)
        pdf = SimpleUploadedFile("receipt.pdf", _PDF, content_type="application/pdf")
        res = login_as(owner).post(
            "/api/v1/expenses/",
            {
                "category": str(category.id),
                "amount": "1500.00",
                "expense_date": "2026-08-27",
                "description": "diesel",
                "receipt_file": pdf,
            },
            format="multipart",
        )
        assert res.status_code == 201, res.content
