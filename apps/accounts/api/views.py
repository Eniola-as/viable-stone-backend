from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.response import Response

from apps.accounts.api.serializers import BranchSerializer, OwnerUserSerializer
from apps.accounts.models import Branch, User
from apps.accounts.services.users import create_staff_user, set_user_active
from apps.core.permissions import IsOwner, IsOwnerOrReadOnly


@extend_schema_view(
    list=extend_schema(summary="List branches", tags=["Branches"]),
    retrieve=extend_schema(summary="Retrieve a branch", tags=["Branches"]),
    create=extend_schema(summary="Create a branch (owner)", tags=["Branches"]),
    update=extend_schema(summary="Update a branch (owner)", tags=["Branches"]),
    partial_update=extend_schema(summary="Patch a branch (owner)", tags=["Branches"]),
)
class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = [IsOwnerOrReadOnly]
    queryset = Branch.objects.all()
    search_fields = ["code", "name"]
    ordering = ["code"]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False):
            return self.queryset
        if user.branch_id is None:
            return self.queryset.none()
        return self.queryset.filter(pk=user.branch_id)


@extend_schema_view(
    list=extend_schema(summary="List staff accounts (owner)", tags=["Users"]),
    retrieve=extend_schema(summary="Retrieve a staff account (owner)", tags=["Users"]),
    create=extend_schema(summary="Create a staff account (owner)", tags=["Users"]),
    update=extend_schema(summary="Update a staff account (owner)", tags=["Users"]),
    partial_update=extend_schema(
        summary="Patch a staff account (owner)", tags=["Users"]
    ),
)
class UserViewSet(viewsets.ModelViewSet):
    """Owner-only staff management, scoped to the owner's branch.

    Accounts are deactivated, never deleted.
    """

    serializer_class = OwnerUserSerializer
    permission_classes = [IsOwner]
    queryset = User.objects.select_related("branch").all()
    search_fields = ["username", "first_name", "last_name", "email"]
    ordering = ["username"]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_queryset(self):
        owner = self.request.user
        return (
            self.queryset.filter(branch_id=owner.branch_id)
            .exclude(role="TECH_ADMIN")
            .exclude(pk=owner.pk)
        )

    def perform_create(self, serializer):
        owner = self.request.user
        data = serializer.validated_data
        serializer.instance = create_staff_user(
            created_by=owner,
            username=data["username"],
            password=data["password"],
            role=data["role"],
            branch=owner.branch,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            email=data.get("email", ""),
            phone=data.get("phone", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        serializer.validated_data.pop("password", None)
        serializer.validated_data.pop("branch", None)
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed(
            "DELETE",
            detail="Accounts are deactivated, not deleted. Use the deactivate action.",
        )

    @extend_schema(request=None, responses={200: OwnerUserSerializer}, tags=["Users"])
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        set_user_active(actor=request.user, user=user, is_active=False, request=request)
        return Response(self.get_serializer(user).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses={200: OwnerUserSerializer}, tags=["Users"])
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        set_user_active(actor=request.user, user=user, is_active=True, request=request)
        return Response(self.get_serializer(user).data, status=status.HTTP_200_OK)
