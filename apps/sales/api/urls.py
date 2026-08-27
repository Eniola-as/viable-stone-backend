from rest_framework.routers import DefaultRouter

from apps.sales.api.views import CustomerViewSet, SaleViewSet

router = DefaultRouter()
router.register("sales", SaleViewSet, basename="sale")
router.register("customers", CustomerViewSet, basename="customer")

urlpatterns = router.urls
