from rest_framework.routers import DefaultRouter

from apps.finance.api.views import (
    ExpenseCategoryViewSet,
    ExpenseViewSet,
    ReportsViewSet,
)

router = DefaultRouter()
router.register(
    "expense-categories", ExpenseCategoryViewSet, basename="expense-category"
)
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("reports", ReportsViewSet, basename="report")

urlpatterns = router.urls
