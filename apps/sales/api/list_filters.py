"""Validated query-parameter filters for sales-app list endpoints.

DRF's ``SearchFilter`` / ``OrderingFilter`` silently drop any query param they
do not own, so an unknown filter used to be ignored rather than rejected. Each
backend below validates its params (standard ``validation_error`` envelope on
bad input, never a silent ignore or a 500), applies exact model filters
AND-combined, and only narrows the ``list`` action — a detail fetch is by id.
They compose with ``search`` / ``ordering`` / ``page`` / ``page_size``.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.filters import BaseFilterBackend

from apps.sales.models import ApprovalStatus, ApprovalType, OfflineSyncOutcome

_APPROVAL_STATUSES = [c[0] for c in ApprovalStatus.choices]
_APPROVAL_TYPES = [c[0] for c in ApprovalType.choices]
_SYNC_OUTCOMES = [c[0] for c in OfflineSyncOutcome.choices]


class _ApprovalParams(serializers.Serializer):
    status = serializers.ChoiceField(choices=ApprovalStatus.choices, required=False)
    request_type = serializers.ChoiceField(choices=ApprovalType.choices, required=False)


class ApprovalFilterBackend(BaseFilterBackend):
    """``?status=`` ``?request_type=`` — exact, AND-combined (G21)."""

    def filter_queryset(self, request, queryset, view):
        params = _ApprovalParams(data=request.query_params)
        params.is_valid(raise_exception=True)
        if getattr(view, "action", None) != "list":
            return queryset
        query = request.query_params
        clean = params.validated_data
        if "status" in query:
            queryset = queryset.filter(status=clean["status"])
        if "request_type" in query:
            queryset = queryset.filter(request_type=clean["request_type"])
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            {
                "name": "status",
                "required": False,
                "in": "query",
                "description": "Filter by approval status (exact).",
                "schema": {"type": "string", "enum": _APPROVAL_STATUSES},
            },
            {
                "name": "request_type",
                "required": False,
                "in": "query",
                "description": "Filter by approval type (exact).",
                "schema": {"type": "string", "enum": _APPROVAL_TYPES},
            },
        ]


class _SyncRecordParams(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=OfflineSyncOutcome.choices, required=False
    )
    resolved = serializers.BooleanField(required=False)


class OfflineSyncRecordFilterBackend(BaseFilterBackend):
    """``?outcome=`` ``?resolved=`` — exact, AND-combined (G3).

    Owner-only + branch scoping are enforced by the viewset's permission class
    and queryset; this backend only narrows the already-scoped list.
    """

    def filter_queryset(self, request, queryset, view):
        params = _SyncRecordParams(data=request.query_params)
        params.is_valid(raise_exception=True)
        if getattr(view, "action", None) != "list":
            return queryset
        query = request.query_params
        clean = params.validated_data
        if "outcome" in query:
            queryset = queryset.filter(outcome=clean["outcome"])
        if "resolved" in query:
            queryset = queryset.filter(resolved=clean["resolved"])
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            {
                "name": "outcome",
                "required": False,
                "in": "query",
                "description": "Filter by per-sale sync outcome (exact).",
                "schema": {"type": "string", "enum": _SYNC_OUTCOMES},
            },
            {
                "name": "resolved",
                "required": False,
                "in": "query",
                "description": "Filter by whether the owner has resolved the record.",
                "schema": {"type": "boolean"},
            },
        ]
