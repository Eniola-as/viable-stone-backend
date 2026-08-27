from rest_framework.routers import DefaultRouter

from apps.accounts.api.views import BranchViewSet, UserViewSet

router = DefaultRouter()
router.register("branches", BranchViewSet, basename="branch")
router.register("users", UserViewSet, basename="user")

urlpatterns = router.urls
