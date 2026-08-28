from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.catalog.api.views import (
    BrandViewSet,
    CategoryViewSet,
    ProductVariantViewSet,
    ProductViewSet,
)

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("brands", BrandViewSet, basename="brand")
router.register("products", ProductViewSet, basename="product")
router.register("variants", ProductVariantViewSet, basename="variant")

# Product image bytes: authenticated + branch-scoped, streamed through storage.
# GET  -> owner or an employee of the product's branch downloads the image.
# DELETE -> owner clears it. Not a router @action so DELETE never reaches the
# ModelViewSet destroy handler (products are retired via is_active, not deleted).
_product_image = ProductViewSet.as_view(
    {"get": "image", "delete": "image_clear"},
    http_method_names=["get", "delete", "head", "options"],
)

urlpatterns = [
    path(
        "products/<uuid:pk>/image/",
        _product_image,
        name="product-image",
    ),
    *router.urls,
]
