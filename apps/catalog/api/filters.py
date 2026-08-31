"""Validated query-parameter filtering for the product list.

DRF's ``SearchFilter`` / ``OrderingFilter`` ignore any query param they do not
own, so ``?category=`` / ``?brand=`` / ``?kind=`` / ``?is_active=`` on
``GET /api/v1/products/`` used to be dropped silently. This backend validates
those params (the standard error envelope on bad input, never a 500) and
applies **exact model-relationship** filters, AND-combined, on top of the
queryset the view has already scoped to the caller's branch and role.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.filters import BaseFilterBackend

from apps.catalog.models import ProductKind

_KINDS = [choice[0] for choice in ProductKind.choices]


class _ProductQueryParams(serializers.Serializer):
    category = serializers.UUIDField(required=False)
    brand = serializers.UUIDField(required=False)
    kind = serializers.ChoiceField(choices=ProductKind.choices, required=False)
    is_active = serializers.BooleanField(required=False)


class ProductFilterBackend(BaseFilterBackend):
    """``?category=`` ``?brand=`` ``?kind=`` ``?is_active=`` — exact, AND-combined.

    Composes with ``search``, ``ordering``, ``page`` and ``page_size`` (each
    handled by its own backend / the paginator).
    """

    def filter_queryset(self, request, queryset, view):
        params = _ProductQueryParams(data=request.query_params)
        params.is_valid(raise_exception=True)  # -> standard validation envelope

        # Validate on every action so a bad value is a 400 everywhere, but only
        # narrow the list — a detail / image / price request is fetched by id.
        if getattr(view, "action", None) != "list":
            return queryset

        query = request.query_params
        clean = params.validated_data
        # Gate on the raw key: DRF BooleanField reads a QueryDict as HTML input,
        # so an *absent* ``is_active`` resolves to False in ``validated_data``.
        if "category" in query:
            queryset = queryset.filter(category_id=clean["category"])
        if "brand" in query:
            queryset = queryset.filter(brand_id=clean["brand"])
        if "kind" in query:
            queryset = queryset.filter(kind=clean["kind"])
        if "is_active" in query:
            # Runs after the view has already forced is_active=True for
            # non-owners, so an employee passing is_active=false just gets an
            # empty page — never a glimpse of an inactive product.
            queryset = queryset.filter(is_active=clean["is_active"])
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            {
                "name": "category",
                "required": False,
                "in": "query",
                "description": "Filter to one category by id (exact match).",
                "schema": {"type": "string", "format": "uuid"},
            },
            {
                "name": "brand",
                "required": False,
                "in": "query",
                "description": "Filter to one brand by id (exact match).",
                "schema": {"type": "string", "format": "uuid"},
            },
            {
                "name": "kind",
                "required": False,
                "in": "query",
                "description": "Filter by product kind.",
                "schema": {"type": "string", "enum": _KINDS},
            },
            {
                "name": "is_active",
                "required": False,
                "in": "query",
                "description": (
                    "Filter by the active flag. Omit for 'any'. Employees only "
                    "ever see active products, so is_active=false returns an "
                    "empty page for them."
                ),
                "schema": {"type": "boolean"},
            },
        ]
