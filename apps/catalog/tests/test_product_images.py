"""Secure product-image delivery (authenticated, branch-scoped, storage-backed).

The product ``image`` is uploaded with ``multipart/form-data`` and stored on the
private storage backend. It is never reachable by a guessable media URL; the
only way to fetch the bytes is
``GET /api/v1/products/{id}/image/`` — authenticated, limited to the product's
branch, and streamed through the configured Django storage so the same code
works for local private storage and a future private S3 bucket.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory
from apps.catalog.models import Product

from .factories import CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/products/"


def _img(fmt: str, *, mode="RGB", size=(12, 10), colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    image = Image.new(mode, size) if mode == "P" else Image.new(mode, size, colour)
    image.save(buf, format=fmt)
    return buf.getvalue()


JPEG = _img("JPEG")
PNG = _img("PNG")
WEBP = _img("WEBP")
GIF = _img("GIF", mode="P", size=(4, 4))
FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _upload(name: str, blob: bytes, content_type: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, blob, content_type=content_type)


def _image_url(product_id) -> str:
    return f"{PRODUCTS}{product_id}/image/"


def _with_image(branch, blob=PNG, name="seed.png") -> Product:
    product = ProductFactory(branch=branch)
    product.image.save(name, ContentFile(blob), save=True)
    return product


def _body(response) -> bytes:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return response.content


# --------------------------------------------------------------------------- #
# Create / replace / clear                                                     #
# --------------------------------------------------------------------------- #


class TestProductImageWrite:
    def test_owner_creates_product_with_image_via_multipart(
        self, login_as, owner, branch
    ):
        category = CategoryFactory(branch=branch)
        res = login_as(owner).post(
            PRODUCTS,
            {
                "category": str(category.id),
                "kind": "PAINT",
                "name": "Red Emulsion",
                "image": _upload("red.jpg", JPEG, "image/jpeg"),
            },
            format="multipart",
        )
        assert res.status_code == 201, res.content
        product = Product.objects.get(id=res.json()["id"])
        assert product.image.name
        assert product.image.storage.exists(product.image.name)

    def test_read_response_exposes_absolute_image_url_and_keeps_image_field(
        self, login_as, owner, branch
    ):
        product = _with_image(branch)
        row = login_as(owner).get(f"{PRODUCTS}{product.id}/").json()
        assert "image" in row  # existing consumers keep the field
        assert row["image_url"] and row["image_url"].startswith("http")
        assert row["image_url"].endswith(_image_url(product.id))

    def test_read_response_image_url_is_null_without_an_image(
        self, login_as, owner, branch
    ):
        product = ProductFactory(branch=branch)
        row = login_as(owner).get(f"{PRODUCTS}{product.id}/").json()
        assert row["image_url"] is None

    def test_owner_replaces_the_image_via_multipart_patch(
        self, login_as, owner, branch
    ):
        product = _with_image(branch, PNG, "old.png")
        res = login_as(owner).patch(
            f"{PRODUCTS}{product.id}/",
            {"image": _upload("new.webp", WEBP, "image/webp")},
            format="multipart",
        )
        assert res.status_code == 200, res.content
        got = login_as(owner).get(_image_url(product.id))
        assert got.status_code == 200
        assert got["Content-Type"] == "image/webp"
        assert _body(got) == WEBP

    def test_owner_clears_the_image_with_delete(self, login_as, owner, branch):
        product = _with_image(branch)
        old_name = product.image.name
        storage = product.image.storage

        res = login_as(owner).delete(_image_url(product.id))
        assert res.status_code == 204

        product.refresh_from_db()
        assert not product.image
        assert not storage.exists(old_name)
        assert login_as(owner).get(_image_url(product.id)).status_code == 404

    def test_employee_cannot_clear_the_image(self, login_as, branch):
        employee = EmployeeFactory(branch=branch)
        product = _with_image(branch)
        res = login_as(employee).delete(_image_url(product.id))
        assert res.status_code == 403
        product.refresh_from_db()
        assert product.image  # untouched

    def test_delete_when_there_is_no_image_is_404(self, login_as, owner, branch):
        product = ProductFactory(branch=branch)
        assert login_as(owner).delete(_image_url(product.id)).status_code == 404


# --------------------------------------------------------------------------- #
# Read: auth, branch isolation, headers, storage abstraction                   #
# --------------------------------------------------------------------------- #


class TestProductImageRead:
    def test_owner_downloads_the_image_with_safe_headers(self, login_as, owner, branch):
        product = _with_image(branch, PNG, "wall.png")
        res = login_as(owner).get(_image_url(product.id))

        assert res.status_code == 200
        assert res["Content-Type"] == "image/png"
        assert res["X-Content-Type-Options"] == "nosniff"
        assert "private" in res["Cache-Control"]
        assert _body(res) == PNG

    def test_same_branch_employee_can_view_the_image(self, login_as, branch):
        employee = EmployeeFactory(branch=branch)
        product = _with_image(branch)
        res = login_as(employee).get(_image_url(product.id))
        assert res.status_code == 200
        assert _body(res) == PNG

    def test_unauthenticated_request_is_401(self, api_client, branch):
        product = _with_image(branch)
        res = api_client.get(_image_url(product.id))
        assert res.status_code == 401

    def test_cross_branch_product_image_is_404(self, login_as, owner, branch):
        other = _with_image(BranchFactory(code="VS77"))
        res = login_as(owner).get(_image_url(other.id))
        assert res.status_code == 404

    def test_unknown_product_image_is_404(self, login_as, owner):
        import uuid

        res = login_as(owner).get(_image_url(uuid.uuid4()))
        assert res.status_code == 404

    def test_product_without_an_image_is_404(self, login_as, owner, branch):
        product = ProductFactory(branch=branch)
        res = login_as(owner).get(_image_url(product.id))
        assert res.status_code == 404

    def test_inactive_product_image_hidden_from_employee_but_visible_to_owner(
        self, login_as, owner, branch
    ):
        employee = EmployeeFactory(branch=branch)
        product = _with_image(branch)
        product.is_active = False
        product.save(update_fields=["is_active"])

        assert login_as(employee).get(_image_url(product.id)).status_code == 404
        assert login_as(owner).get(_image_url(product.id)).status_code == 200

    def test_no_filesystem_path_or_secret_leaks_in_the_response(
        self, login_as, owner, branch, settings
    ):
        product = _with_image(branch, JPEG, "leaky.jpg")
        res = login_as(owner).get(_image_url(product.id))

        disposition = res.get("Content-Disposition", "")
        assert "/" not in disposition and "\\" not in disposition
        assert str(settings.PRIVATE_MEDIA_ROOT) not in disposition
        blob = " ".join(f"{k}: {v}" for k, v in res.items())
        assert str(settings.PRIVATE_MEDIA_ROOT) not in blob
        assert "products/" not in disposition  # only the bare filename

    def test_missing_image_404_body_does_not_leak_a_path(
        self, login_as, owner, branch, settings
    ):
        product = ProductFactory(branch=branch)
        res = login_as(owner).get(_image_url(product.id))
        assert res.status_code == 404
        assert str(settings.PRIVATE_MEDIA_ROOT) not in res.content.decode()

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
    )
    def test_image_is_streamed_through_storage_not_a_filesystem_path(
        self, login_as, owner, branch
    ):
        """A non-filesystem storage (``.path()`` raises, like S3) must still work."""

        product = ProductFactory(branch=branch)
        product.image.save("mem.png", ContentFile(PNG), save=True)
        assert isinstance(product.image.storage, InMemoryStorage)

        res = login_as(owner).get(_image_url(product.id))
        assert res.status_code == 200
        assert res["Content-Type"] == "image/png"
        assert _body(res) == PNG


# --------------------------------------------------------------------------- #
# Upload validation: type, disguised content, size                             #
# --------------------------------------------------------------------------- #


class TestProductImageValidation:
    def _create(self, client, branch, upload):
        category = CategoryFactory(branch=branch)
        return client.post(
            PRODUCTS,
            {
                "category": str(category.id),
                "kind": "PAINT",
                "name": "Probe",
                "image": upload,
            },
            format="multipart",
        )

    @pytest.mark.parametrize(
        ("name", "blob", "ct"),
        [
            ("a.jpg", JPEG, "image/jpeg"),
            ("a.jpeg", JPEG, "image/jpeg"),
            ("a.png", PNG, "image/png"),
            ("a.webp", WEBP, "image/webp"),
        ],
    )
    def test_accepts_jpeg_png_webp(self, login_as, owner, branch, name, blob, ct):
        res = self._create(login_as(owner), branch, _upload(name, blob, ct))
        assert res.status_code == 201, res.content

    def test_rejects_a_pdf_disguised_as_jpg(self, login_as, owner, branch):
        res = self._create(
            login_as(owner), branch, _upload("invoice.jpg", FAKE_PDF, "image/jpeg")
        )
        assert res.status_code == 400
        assert "image" in res.json()["field_errors"]

    def test_rejects_a_real_image_with_a_mismatched_extension(
        self, login_as, owner, branch
    ):
        res = self._create(
            login_as(owner), branch, _upload("shot.png", JPEG, "image/png")
        )
        assert res.status_code == 400
        assert "image" in res.json()["field_errors"]

    def test_rejects_an_unsupported_image_type(self, login_as, owner, branch):
        res = self._create(
            login_as(owner), branch, _upload("anim.gif", GIF, "image/gif")
        )
        assert res.status_code == 400
        assert "image" in res.json()["field_errors"]

    @override_settings(PRODUCT_IMAGE_MAX_BYTES=512)
    def test_rejects_an_image_over_the_documented_size_limit(
        self, login_as, owner, branch
    ):
        big = _img("PNG", size=(400, 400))
        assert len(big) > 512
        res = self._create(
            login_as(owner), branch, _upload("big.png", big, "image/png")
        )
        assert res.status_code == 400
        assert "image" in res.json()["field_errors"]


class TestValidateProductImageUnit:
    """The validator checks real bytes, not the filename or the browser MIME."""

    def test_disguised_pdf_is_rejected(self):
        from rest_framework import serializers

        from apps.catalog.validators import validate_product_image

        with pytest.raises(serializers.ValidationError):
            validate_product_image(_upload("x.jpg", FAKE_PDF, "image/jpeg"))

    def test_valid_png_passes(self):
        from apps.catalog.validators import validate_product_image

        validate_product_image(_upload("x.png", PNG, "image/png"))

    def test_oversize_is_rejected(self):
        from rest_framework import serializers

        from apps.catalog.validators import validate_product_image

        with override_settings(PRODUCT_IMAGE_MAX_BYTES=16):
            with pytest.raises(serializers.ValidationError):
                validate_product_image(_upload("x.png", PNG, "image/png"))
