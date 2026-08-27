from rest_framework.routers import DefaultRouter

from apps.inventory.api.views import (
    InventoryViewSet,
    RestockViewSet,
    StockCountViewSet,
    SupplierViewSet,
)

router = DefaultRouter()
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("restocks", RestockViewSet, basename="restock")
router.register("stock-counts", StockCountViewSet, basename="stock-count")
router.register("inventory", InventoryViewSet, basename="inventory")

urlpatterns = router.urls
