from django.db.models import Sum
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.response import Response

from apps.core.mixins import AuditCreateUpdateMixin, BranchScopedQuerysetMixin
from apps.core.money import to_money
from apps.core.permissions import IsOwner
from apps.finance.api.serializers import (
    BestSellerSerializer,
    ExpenseCategorySerializer,
    ExpenseSerializer,
    ExpenseVoidSerializer,
    InventorySummarySerializer,
    ProfitReportSerializer,
    SlowMoverSerializer,
)
from apps.finance.models import Expense, ExpenseCategory
from apps.finance.services.expenses import void_expense
from apps.finance.services.reports import (
    best_sellers,
    profit_report,
    slow_movers,
)
from apps.inventory.models import InventoryBalance
from apps.inventory.selectors import low_stock_for_branch, stock_value_for_branch

_NO_DELETE = ["get", "post", "put", "patch", "head", "options"]

_RANGE_PARAMS = [
    OpenApiParameter("period", str, description="today | week | month | custom"),
    OpenApiParameter("start", str, description="ISO date (custom period)"),
    OpenApiParameter("end", str, description="ISO date, inclusive (custom period)"),
    OpenApiParameter("limit", int, description="max rows (best/slow sellers)"),
]


@extend_schema_view(
    list=extend_schema(summary="List expense categories (owner)", tags=["Finance"]),
    create=extend_schema(
        summary="Create an expense category (owner)", tags=["Finance"]
    ),
)
class ExpenseCategoryViewSet(
    BranchScopedQuerysetMixin, AuditCreateUpdateMixin, viewsets.ModelViewSet
):
    serializer_class = ExpenseCategorySerializer
    permission_classes = [IsOwner]
    queryset = ExpenseCategory.objects.all()
    search_fields = ["name"]
    http_method_names = _NO_DELETE
    audit_action_prefix = "expense_category"

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch)
        super().perform_create(serializer)


@extend_schema_view(
    list=extend_schema(summary="List expenses (owner)", tags=["Finance"]),
    retrieve=extend_schema(summary="Retrieve an expense (owner)", tags=["Finance"]),
    create=extend_schema(summary="Record an expense (owner)", tags=["Finance"]),
)
class ExpenseViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    permission_classes = [IsOwner]
    queryset = Expense.objects.select_related("category", "created_by")
    search_fields = ["description", "category__name"]
    ordering = ["-expense_date"]
    http_method_names = _NO_DELETE

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch, created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed(
            "DELETE", detail="Expenses are voided, not deleted. Use the void action."
        )

    @extend_schema(
        request=ExpenseVoidSerializer,
        responses={200: ExpenseSerializer},
        summary="Void an expense (owner)",
        tags=["Finance"],
    )
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        serializer = ExpenseVoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expense = void_expense(
            expense=self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(ExpenseSerializer(expense).data)


class ReportsViewSet(viewsets.GenericViewSet):
    """Owner-only computed reports. No cost/profit ever reaches an employee.

    None of these actions paginate. ``profit`` / ``inventory`` return one
    summary object; ``best-sellers`` / ``slow-movers`` return a **bare array**
    capped by ``?limit`` (1..100, default 10 / 20) — ``page`` / ``page_size``
    are ignored (G18 / G13). ``pagination_class = None`` keeps the generated
    schema honest.
    """

    permission_classes = [IsOwner]
    pagination_class = None
    queryset = Expense.objects.none()  # satisfies router; unused

    def _range_kwargs(self, request):
        return {
            "period": request.query_params.get("period", "today"),
            "start": request.query_params.get("start"),
            "end": request.query_params.get("end"),
        }

    def _limit(self, request, default=10):
        try:
            return max(1, min(100, int(request.query_params.get("limit", default))))
        except (TypeError, ValueError):
            return default

    @extend_schema(
        parameters=_RANGE_PARAMS,
        responses={200: ProfitReportSerializer},
        summary="Revenue / COGS / gross & net profit for a period",
        tags=["Reports"],
    )
    @action(detail=False)
    def profit(self, request):
        report = profit_report(
            branch=request.user.branch, **self._range_kwargs(request)
        )
        return Response(ProfitReportSerializer(report).data)

    @extend_schema(
        parameters=_RANGE_PARAMS,
        responses={200: BestSellerSerializer(many=True)},
        summary="Best-selling variants for a period",
        tags=["Reports"],
    )
    @action(detail=False, url_path="best-sellers")
    def best_sellers(self, request):
        rows = best_sellers(
            branch=request.user.branch,
            limit=self._limit(request, 10),
            **self._range_kwargs(request),
        )
        return Response(BestSellerSerializer(rows, many=True).data)

    @extend_schema(
        parameters=_RANGE_PARAMS,
        responses={200: SlowMoverSerializer(many=True)},
        summary="In-stock variants with no sales in a period",
        tags=["Reports"],
    )
    @action(detail=False, url_path="slow-movers")
    def slow_movers(self, request):
        rows = slow_movers(
            branch=request.user.branch,
            limit=self._limit(request, 20),
            **self._range_kwargs(request),
        )
        return Response(SlowMoverSerializer(rows, many=True).data)

    @extend_schema(
        responses={200: InventorySummarySerializer},
        summary="Stock value and low-stock summary (owner)",
        tags=["Reports"],
    )
    @action(detail=False)
    def inventory(self, request):
        branch_id = request.user.branch_id
        agg = InventoryBalance.objects.filter(
            branch_id=branch_id, quantity__gt=0
        ).aggregate(units=Sum("quantity"))
        data = {
            "stock_value": to_money(stock_value_for_branch(branch_id)),
            "low_stock_count": low_stock_for_branch(branch_id).count(),
            "distinct_variants_in_stock": InventoryBalance.objects.filter(
                branch_id=branch_id, quantity__gt=0
            ).count(),
            "total_units_in_stock": agg["units"] or 0,
        }
        return Response(InventorySummarySerializer(data).data)
