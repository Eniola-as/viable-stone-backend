from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.sales.api.views import CustomerViewSet, SaleViewSet

router = DefaultRouter()
router.register("sales", SaleViewSet, basename="sale")
router.register("customers", CustomerViewSet, basename="customer")

# Explicit receipt routes so "receipt.pdf" is a literal path segment, not a DRF
# format suffix on "receipt".
_receipt = SaleViewSet.as_view({"get": "receipt"})
_receipt_pdf = SaleViewSet.as_view({"get": "receipt_pdf"})

urlpatterns = [
    path("sales/<uuid:pk>/receipt/", _receipt, name="sale-receipt"),
    path("sales/<uuid:pk>/receipt.pdf", _receipt_pdf, name="sale-receipt-pdf"),
    *router.urls,
]
