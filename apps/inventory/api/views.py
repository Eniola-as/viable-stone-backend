from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.catalog.models import ProductVariant
from apps.core.exceptions import APIError, Conflict
from apps.core.mixins import AuditCreateUpdateMixin, BranchScopedQuerysetMixin
from apps.core.money import to_money
from apps.core.permissions import IsOwner
from apps.inventory.api.serializers import (
    InventoryBalanceEmployeeSerializer,
    InventoryBalanceOwnerSerializer,
    OpeningStockSerializer,
    RestockReadSerializer,
    RestockWriteSerializer,
    StockAdjustmentSerializer,
    StockCountReadSerializer,
    StockCountWriteSerializer,
    StockMovementSerializer,
    StockValueSerializer,
    SupplierSerializer,
)
from apps.inventory.models import (
    InventoryBalance,
    Restock,
    RestockStatus,
    StockCount,
    Supplier,
)
from apps.inventory.selectors import (
    balances_for_branch,
    low_stock_for_branch,
    movements_for_variant,
    stock_value_for_branch,
)
from apps.inventory.services.adjustments import adjust_stock
from apps.inventory.services.restock import confirm_restock, restock_purchase_total
from apps.inventory.services.stock import open_stock
from apps.inventory.services.stock_count import apply_stock_count, submit_stock_count

_NO_DELETE = ["get", "post", "put", "patch", "head", "options"]


@extend_schema_view(
    list=extend_schema(summary="List suppliers (owner)", tags=["Inventory"]),
    create=extend_schema(summary="Create a supplier (owner)", tags=["Inventory"]),
)
class SupplierViewSet(
    BranchScopedQuerysetMixin, AuditCreateUpdateMixin, viewsets.ModelViewSet
):
    """Owner-only. Employees never receive supplier data. Deactivate, don't delete."""

    serializer_class = SupplierSerializer
    permission_classes = [IsOwner]
    queryset = Supplier.objects.all()
    search_fields = ["name", "contact_name"]
    http_method_names = _NO_DELETE
    audit_action_prefix = "supplier"

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch)
        super().perform_create(serializer)


@extend_schema_view(
    list=extend_schema(summary="List restocks (owner)", tags=["Inventory"]),
    retrieve=extend_schema(summary="Retrieve a restock (owner)", tags=["Inventory"]),
    create=extend_schema(summary="Create a draft restock (owner)", tags=["Inventory"]),
)
class RestockViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    permission_classes = [IsOwner]
    queryset = Restock.objects.select_related(
        "supplier", "created_by"
    ).prefetch_related("items__variant")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    ordering = ["-date"]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return RestockWriteSerializer
        return RestockReadSerializer

    def _reject_if_confirmed(self):
        obj = self.get_object()
        if obj.status == RestockStatus.CONFIRMED:
            raise Conflict(
                "A confirmed restock is immutable.", code="restock_immutable"
            )
        return obj

    def update(self, request, *args, **kwargs):
        self._reject_if_confirmed()
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        restock = serializer.save()
        # Keep the header total consistent with whatever items the draft holds.
        new_total = restock_purchase_total(restock)
        if restock.total_cost != new_total:
            restock.total_cost = new_total
            restock.save(update_fields=["total_cost", "updated_at"])

    def destroy(self, request, *args, **kwargs):
        self._reject_if_confirmed()
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={200: RestockReadSerializer},
        summary="Confirm a restock — applies stock and weighted cost once",
        tags=["Inventory"],
    )
    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        restock = self.get_object()
        confirmed = confirm_restock(
            restock=restock, confirmed_by=request.user, request=request
        )
        return Response(RestockReadSerializer(confirmed).data)


@extend_schema_view(
    list=extend_schema(summary="Current stock balances", tags=["Inventory"]),
)
class InventoryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Branch stock. Cashiers see quantity + price; owner also sees cost."""

    queryset = None  # set via get_queryset

    def get_serializer_class(self):
        if getattr(self.request.user, "is_owner", False):
            return InventoryBalanceOwnerSerializer
        return InventoryBalanceEmployeeSerializer

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_tech_admin", False) or user.branch_id is None:
            return InventoryBalance.objects.none()
        return balances_for_branch(user.branch_id)

    @extend_schema(
        operation_id="inventory_low_stock_retrieve",
        responses={200: InventoryBalanceEmployeeSerializer(many=True)},
        summary="Low-stock balances (paginated)",
        description=(
            "Branch balances at or below each variant's low-stock level. "
            "Paginated. Cashiers get quantity + price; an owner also gets cost "
            "fields at runtime (the schema documents the guaranteed subset)."
        ),
        tags=["Inventory"],
    )
    @action(detail=False, url_path="low-stock")
    def low_stock(self, request):
        queryset = low_stock_for_branch(request.user.branch_id)
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(
        operation_id="inventory_stock_value_retrieve",
        responses={200: StockValueSerializer},
        summary="Total stock value at cost (owner)",
        tags=["Inventory"],
    )
    @action(detail=False, url_path="stock-value", permission_classes=[IsOwner])
    def stock_value(self, request):
        total = to_money(stock_value_for_branch(request.user.branch_id))
        return Response(StockValueSerializer({"stock_value": str(total)}).data)

    @extend_schema(
        request=OpeningStockSerializer,
        responses={201: InventoryBalanceOwnerSerializer},
        summary="Establish opening stock (owner, before any other movement)",
        tags=["Inventory"],
    )
    @action(detail=False, methods=["post"], permission_classes=[IsOwner])
    def opening(self, request):
        serializer = OpeningStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant = serializer.validated_data["variant"]
        if variant.product.branch_id != request.user.branch_id:
            raise APIError("Unknown variant for this branch.", code="variant_not_found")
        balance = open_stock(
            branch=request.user.branch,
            variant=variant,
            quantity=serializer.validated_data["quantity"],
            unit_cost=serializer.validated_data["unit_cost"],
            created_by=request.user,
            request=request,
        )
        return Response(InventoryBalanceOwnerSerializer(balance).data, status=201)

    @extend_schema(
        request=StockAdjustmentSerializer,
        responses={201: InventoryBalanceOwnerSerializer},
        summary="Protected stock adjustment (owner)",
        tags=["Inventory"],
    )
    @action(detail=False, methods=["post"], permission_classes=[IsOwner])
    def adjustments(self, request):
        serializer = StockAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        variant = ProductVariant.objects.filter(
            pk=data["variant"], product__branch_id=request.user.branch_id
        ).first()
        if variant is None:
            raise NotFound("Unknown variant for this branch.")
        balance = adjust_stock(
            branch=request.user.branch,
            variant=variant,
            direction=data["direction"],
            quantity=data["quantity"],
            reason=data["reason"],
            actor=request.user,
            client_adjustment_id=data["client_adjustment_id"],
            request=request,
        )
        return Response(InventoryBalanceOwnerSerializer(balance).data, status=201)

    @extend_schema(
        operation_id="inventory_movements_retrieve",
        parameters=[
            OpenApiParameter(
                "variant",
                str,
                OpenApiParameter.QUERY,
                required=True,
                description="Variant id whose stock-movement ledger to return.",
            ),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: StockMovementSerializer(many=True)},
        summary="Stock-movement ledger for one variant (owner, paginated)",
        description=(
            "Append-only stock-movement ledger for one variant, newest first, "
            "paginated. Owner only; `?variant=<id>` is required. Each row: "
            "movement type, quantity delta, cost snapshot, actor, reference "
            "type/id and timestamp."
        ),
        tags=["Inventory"],
    )
    @action(detail=False, permission_classes=[IsOwner])
    def movements(self, request):
        variant_id = request.query_params.get("variant")
        if not variant_id:
            raise APIError(
                "Provide ?variant=<id>.",
                code="variant_required",
                field_errors={"variant": ["This query parameter is required."]},
            )
        if not ProductVariant.objects.filter(
            pk=variant_id, product__branch_id=request.user.branch_id
        ).exists():
            raise NotFound("Unknown variant for this branch.")
        queryset = movements_for_variant(request.user.branch_id, variant_id)
        page = self.paginate_queryset(queryset)
        serializer = StockMovementSerializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(summary="List stock counts (owner)", tags=["Inventory"]),
    create=extend_schema(
        summary="Create a draft stock count (owner)", tags=["Inventory"]
    ),
)
class StockCountViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    permission_classes = [IsOwner]
    queryset = StockCount.objects.prefetch_related("items__variant")
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return StockCountWriteSerializer
        return StockCountReadSerializer

    @extend_schema(
        request=None, responses={200: StockCountReadSerializer}, tags=["Inventory"]
    )
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        count = submit_stock_count(
            stock_count=self.get_object(), actor=request.user, request=request
        )
        return Response(StockCountReadSerializer(count).data)

    @extend_schema(
        request=None, responses={200: StockCountReadSerializer}, tags=["Inventory"]
    )
    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        count = apply_stock_count(
            stock_count=self.get_object(), applied_by=request.user, request=request
        )
        return Response(StockCountReadSerializer(count).data)

    def perform_create(self, serializer):
        serializer.save()
