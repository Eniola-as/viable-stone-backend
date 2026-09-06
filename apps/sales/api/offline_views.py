"""Offline fixed-price checkout API.

Device + authorization lifecycle is owner + MFA only. The batch sync and
temporary-receipt endpoints are cashier-authenticated and device-bound through
the signed authorization token. Cross-device / cross-branch access returns 404.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import (
    DeviceStatus,
    OfflineDeviceAuthorization,
    RegisteredDevice,
)
from apps.core.exceptions import APIError, Conflict
from apps.core.permissions import IsAuthenticatedAndMFAVerified, IsOwner
from apps.core.services.audit import record_audit
from apps.sales.api.list_filters import OfflineSyncRecordFilterBackend
from apps.sales.api.offline_serializers import (
    OfflineAuthorizationCreateSerializer,
    OfflineAuthorizationReplaceSerializer,
    OfflineAuthorizationRevokeSerializer,
    OfflineAuthorizationSerializer,
    OfflineAuthorizationStatusSerializer,
    OfflineCatalogueSnapshotSerializer,
    OfflineDeviceCreateSerializer,
    OfflineDeviceSerializer,
    OfflineReconcileRequestSerializer,
    OfflineReconciliationResultSerializer,
    OfflineSaleLookupSerializer,
    OfflineSessionEndSerializer,
    OfflineSyncRecordDetailSerializer,
    OfflineSyncRecordResolveSerializer,
    OfflineSyncRecordSerializer,
    OfflineSyncRequestSerializer,
    OfflineSyncResponseSerializer,
    OfflineTemporaryReceiptRequestSerializer,
)
from apps.sales.models import OfflineSaleSyncRecord
from apps.sales.services import offline as offline_service
from apps.sales.services import offline_reconciliation
from apps.sales.services.offline import (
    OfflinePaymentInput,
    OfflineSaleInput,
)


def _branch(request):
    return request.user.branch


def _sale_inputs(rows):
    return [
        OfflineSaleInput(
            client_sale_id=row["client_sale_id"],
            device_sequence=row["device_sequence"],
            offline_created_at=row["offline_created_at"],
            items=[dict(i) for i in row["items"]],
            payments=[
                OfflinePaymentInput(
                    method=p["method"],
                    amount=p["amount"],
                    tendered_amount=p.get("tendered_amount"),
                    reference=p.get("reference", ""),
                )
                for p in row["payments"]
            ],
            customer_name=row.get("customer_name", ""),
            customer_phone=row.get("customer_phone", ""),
        )
        for row in rows
    ]


@extend_schema_view(
    list=extend_schema(summary="List offline devices (owner)", tags=["Offline"]),
    retrieve=extend_schema(summary="Retrieve an offline device", tags=["Offline"]),
)
class OfflineDeviceViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = OfflineDeviceSerializer
    permission_classes = [IsOwner]
    queryset = RegisteredDevice.objects.none()

    def get_queryset(self):
        return RegisteredDevice.objects.filter(branch_id=self.request.user.branch_id)

    def get_throttles(self):
        if self.action in {"create", "revoke", "replace"}:
            self.throttle_scope = "offline_sync"
        return super().get_throttles()

    @extend_schema(
        request=OfflineDeviceCreateSerializer,
        responses={201: OfflineDeviceSerializer},
        summary="Register the branch's offline device (owner + MFA)",
        tags=["Offline"],
    )
    def create(self, request, *args, **kwargs):
        serializer = OfflineDeviceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            device = RegisteredDevice.objects.create(
                branch=request.user.branch,
                name=serializer.validated_data["name"],
                registered_by=request.user,
            )
        except IntegrityError as err:
            raise Conflict(
                "This branch already has an active offline device.",
                code="active_device_exists",
            ) from err
        record_audit(
            action="offline.device_register",
            target=device,
            request=request,
            after={"name": device.name},
        )
        return Response(
            OfflineDeviceSerializer(device).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        request=None,
        responses={200: OfflineDeviceSerializer},
        summary="Revoke an offline device (owner + MFA)",
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        device = self.get_object()
        device.status = DeviceStatus.REVOKED
        device.revoked_at = timezone.now()
        device.save(update_fields=["status", "revoked_at", "updated_at"])
        record_audit(action="offline.device_revoke", target=device, request=request)
        return Response(OfflineDeviceSerializer(device).data)

    @extend_schema(
        request=OfflineDeviceCreateSerializer,
        responses={201: OfflineDeviceSerializer},
        summary="Replace an offline device (owner + MFA)",
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        old = self.get_object()
        serializer = OfflineDeviceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        old.status = DeviceStatus.REPLACED
        old.revoked_at = timezone.now()
        old.save(update_fields=["status", "revoked_at", "updated_at"])
        device = RegisteredDevice.objects.create(
            branch=request.user.branch,
            name=serializer.validated_data["name"],
            registered_by=request.user,
        )
        record_audit(
            action="offline.device_replace",
            target=device,
            request=request,
            after={"replaced": str(old.id)},
        )
        return Response(
            OfflineDeviceSerializer(device).data, status=status.HTTP_201_CREATED
        )


@extend_schema_view(
    list=extend_schema(summary="List offline authorizations", tags=["Offline"]),
    retrieve=extend_schema(
        summary="Retrieve an offline authorization", tags=["Offline"]
    ),
)
class OfflineAuthorizationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = OfflineAuthorizationSerializer
    permission_classes = [IsAuthenticatedAndMFAVerified]
    queryset = OfflineDeviceAuthorization.objects.none()

    def get_queryset(self):
        user = self.request.user
        if user.branch_id is None:
            return OfflineDeviceAuthorization.objects.none()
        return OfflineDeviceAuthorization.objects.filter(
            branch_id=user.branch_id
        ).select_related("device", "cashier")

    def get_permissions(self):
        if self.action in {"create", "revoke", "replace", "end_session"}:
            return [IsOwner()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action in {"create", "revoke", "replace", "end_session"}:
            self.throttle_scope = "offline_sync"
        return super().get_throttles()

    @extend_schema(
        request=OfflineAuthorizationCreateSerializer,
        responses={201: OfflineAuthorizationSerializer},
        summary="Authorise the offline session (owner + MFA)",
        tags=["Offline"],
    )
    def create(self, request, *args, **kwargs):
        serializer = OfflineAuthorizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = get_object_or_404(
            RegisteredDevice.objects.filter(branch_id=request.user.branch_id),
            pk=serializer.validated_data["device"],
        )
        cashier = get_object_or_404(
            request.user.branch.users.all(),
            pk=serializer.validated_data["cashier"],
        )
        authorization = offline_service.issue_offline_authorization(
            branch=request.user.branch,
            device=device,
            cashier=cashier,
            owner=request.user,
            request=request,
        )
        return Response(
            OfflineAuthorizationSerializer(authorization).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=OfflineAuthorizationRevokeSerializer,
        responses={200: OfflineAuthorizationSerializer},
        summary="Revoke an offline authorization (owner + MFA)",
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        authorization = self.get_object()
        serializer = OfflineAuthorizationRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = offline_service.revoke_offline_authorization(
            authorization=authorization,
            owner=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(OfflineAuthorizationSerializer(updated).data)

    @extend_schema(
        request=OfflineAuthorizationReplaceSerializer,
        responses={201: OfflineAuthorizationSerializer},
        summary="Replace an offline authorization (owner + MFA)",
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        authorization = self.get_object()
        serializer = OfflineAuthorizationReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = None
        cashier = None
        if serializer.validated_data.get("device"):
            device = get_object_or_404(
                RegisteredDevice.objects.filter(branch_id=request.user.branch_id),
                pk=serializer.validated_data["device"],
            )
        if serializer.validated_data.get("cashier"):
            cashier = get_object_or_404(
                request.user.branch.users.all(),
                pk=serializer.validated_data["cashier"],
            )
        fresh = offline_service.replace_offline_authorization(
            authorization=authorization,
            owner=request.user,
            device=device,
            cashier=cashier,
            request=request,
        )
        return Response(
            OfflineAuthorizationSerializer(fresh).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=OfflineSessionEndSerializer,
        responses={200: OfflineAuthorizationSerializer},
        summary="End the offline session (force-end needs MFA confirmation)",
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"], url_path="end-session")
    def end_session(self, request, pk=None):
        authorization = self.get_object()
        serializer = OfflineSessionEndSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["force"] and not data["mfa_confirmed"]:
            raise APIError(
                "Force-ending a session with pending sales needs MFA confirmation.",
                code="mfa_confirmation_required",
            )
        updated = offline_service.end_offline_session(
            authorization=authorization,
            owner=request.user,
            force=data["force"],
            reason=data.get("reason", ""),
            request=request,
        )
        return Response(OfflineAuthorizationSerializer(updated).data)

    @extend_schema(
        responses={200: OfflineAuthorizationStatusSerializer},
        summary="Authorization expiry and pending-sync count",
        tags=["Offline"],
    )
    @action(detail=True, methods=["get"], url_path="status")
    def session_status(self, request, pk=None):
        authorization = self.get_object()
        return Response(offline_service.authorization_status(authorization))

    @extend_schema(
        responses={200: OfflineCatalogueSnapshotSerializer},
        summary="Read the active session's signed fixed-price catalogue snapshot",
        description=(
            "The disconnected offline device reads this once at session start to "
            "build sales against fixed prices and quantities, then presents the "
            "returned `signed_token` back to `/offline/sync/` and "
            "`/offline/temporary-receipt/` as `authorization_token`. The snapshot "
            "is frozen for the life of the session — repeated reads are identical "
            "and never pick up newer prices or stock. Response is "
            "`Cache-Control: private, no-store`.\n\n"
            "Binding is proven server-side: the GET carries no body and no "
            "client token; the server re-verifies its own stored `signed_token` "
            "(the same HMAC + constant-time check `/offline/sync/` uses) and the "
            "verified branch / device / cashier must match the row and the "
            "caller, and the bound device must still be the branch's one ACTIVE "
            "registered device.\n\n"
            "**404 (indistinguishable, reveals nothing):** wrong cashier, wrong "
            "branch, unknown id, or the bound device is no longer the active "
            "registered device. **409 standard error envelope (the session is "
            "provably yours but over):** `offline_authorization_expired` when "
            "expired, `offline_session_not_active` when revoked / replaced / "
            "force-closed / closed. **400 `invalid_signature`:** the stored "
            "token fails verification."
        ),
        tags=["Offline"],
    )
    @action(detail=True, methods=["get"], url_path="snapshot")
    def snapshot(self, request, pk=None):
        authorization = self.get_object()
        try:
            payload = offline_service.catalogue_snapshot_for_cashier(
                authorization=authorization, user=request.user
            )
        except offline_service._NotFound as exc:
            raise NotFound("No such offline authorization.") from exc
        response = Response(OfflineCatalogueSnapshotSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        return response


class _TokenBoundView(APIView):
    permission_classes = [IsAuthenticatedAndMFAVerified]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "offline_sync"

    def _load(self, request, token):
        try:
            return offline_service.load_authorization_for_token(
                token=token, branch=request.user.branch, user=request.user
            )
        except offline_service._NotFound as exc:
            raise NotFound("No such offline authorization.") from exc


class OfflineSyncView(_TokenBoundView):
    @extend_schema(
        request=OfflineSyncRequestSerializer,
        responses={200: OfflineSyncResponseSerializer},
        summary="Synchronise a batch of offline sales (device-bound)",
        description=(
            'Returns one object — `{ "results": [OfflineSyncResult, ...] }` — '
            "with exactly one entry per submitted sale, in device-sequence "
            "order. It is never a bare array, even for a single-sale batch."
        ),
        tags=["Offline"],
    )
    def post(self, request):
        serializer = OfflineSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        authorization, snapshot = self._load(
            request, serializer.validated_data["authorization_token"]
        )
        results = offline_service.sync_offline_batch(
            branch=request.user.branch,
            authorization=authorization,
            snapshot=snapshot,
            sales=_sale_inputs(serializer.validated_data["sales"]),
            request=request,
        )
        return Response(
            OfflineSyncResponseSerializer(
                {"results": [r.__dict__ for r in results]}
            ).data
        )


class OfflineTemporaryReceiptView(_TokenBoundView):
    @extend_schema(
        request=OfflineTemporaryReceiptRequestSerializer,
        responses={200: dict},
        summary="Build a temporary offline receipt from the signed snapshot",
        tags=["Offline"],
    )
    def post(self, request):
        serializer = OfflineTemporaryReceiptRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _authorization, snapshot = self._load(
            request, serializer.validated_data["authorization_token"]
        )
        [sale_input] = _sale_inputs([serializer.validated_data["sale"]])
        return Response(
            offline_service.temporary_receipt(sale_input=sale_input, snapshot=snapshot)
        )


@extend_schema_view(
    list=extend_schema(summary="List offline sync records (owner)", tags=["Offline"]),
    retrieve=extend_schema(
        summary="Retrieve an offline sync record (owner)",
        description=(
            "Single record. Adds the owner-only pre-confirm diagnostics "
            "`verified_snapshot_total` (catalogue price-times-quantity total "
            "from the verified snapshot) and `retained_payments_total` (sum of "
            "the retained device payments) so the owner can see catalogue-vs-"
            "collected divergence **before** attesting a `REFUNDED_AND_RETURNED`."
            " Either is `null` when it cannot be established. Neither is on the "
            "list response or the cashier `OfflineSaleLookup`."
        ),
        tags=["Offline"],
    ),
)
class OfflineSyncRecordViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = OfflineSyncRecordSerializer
    permission_classes = [IsOwner]
    queryset = OfflineSaleSyncRecord.objects.none()
    # ``?outcome=`` / ``?resolved=`` are validated + documented by this backend
    # (G3); it only narrows the already branch-scoped, owner-only list.
    filter_backends = [
        OfflineSyncRecordFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    def get_queryset(self):
        return OfflineSaleSyncRecord.objects.filter(
            branch_id=self.request.user.branch_id
        ).select_related("sale", "authorization")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return OfflineSyncRecordDetailSerializer
        return OfflineSyncRecordSerializer

    @extend_schema(
        request=OfflineSyncRecordResolveSerializer,
        responses={200: OfflineSyncRecordSerializer},
        summary="DEPRECATED note-only resolve — use reconcile (owner)",
        description=(
            "A CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED record affects stock, "
            "revenue, payments and COGS and can no longer be closed with a note: "
            "this returns `409 offline_reconciliation_required`. Use "
            "`POST /api/v1/offline/sync-records/{id}/reconcile/`. Historical "
            "already-resolved records are unaffected."
        ),
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        record = self.get_object()
        serializer = OfflineSyncRecordResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = offline_service.resolve_sync_record(
            record=record,
            owner=request.user,
            note=serializer.validated_data["note"],
            request=request,
        )
        return Response(OfflineSyncRecordSerializer(updated).data)

    @extend_schema(
        request=OfflineReconcileRequestSerializer,
        responses={200: OfflineReconciliationResultSerializer},
        summary="Reconcile an accounting-impacting sync record (owner + MFA)",
        description=(
            "The safe resolution of a CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED "
            "record — no Sale, stock, payment, revenue or COGS exists for it yet. "
            "Polymorphic on `kind`:\n\n"
            "* `RECORDED_AS_SALE` — the backend re-reads the retained payload and "
            "the *verified* signed snapshot and creates the official Sale itself "
            "(authoritative prices / COGS / one receipt). Needs `counts` (one per "
            "affected variant) when the outcome is `CONFLICT` or stock is short. "
            "`payment_references` is a **structured** list — "
            "`[{ payment_index, reference }]` — where `payment_index` is the "
            "0-based position in the record's `retained_payments` read model; one "
            "entry per retained Transfer/POS line, so a split batch is never "
            "mis-mapped. `amount_source` on the result is always "
            "`SNAPSHOT_VERIFIED` (a Sale is only built from a verified total).\n"
            "* `REFUNDED_AND_RETURNED` — full return + full refund only; no Sale "
            "/ Payment / stock movement is created. `refunds` must total the "
            "money **actually collected** from the customer — NOT the catalogue "
            "total. That amount is trusted automatically only when the retained "
            "device payments parse cleanly and equal the verified snapshot total "
            "(`amount_source: RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT`). If they "
            "disagree, or the retained data cannot be verified (broken "
            "signature / binding, malformed items), the request must carry "
            "`owner_attestation: true` + `attested_offline_total` (the amount "
            "actually collected) + a ≥40-char `explanation`; the result is "
            "flagged `amount_source: OWNER_ATTESTED` (never presented as "
            "cryptographically verified). A non-positive collected amount -> "
            "`no_payment_to_refund` (it is not a refund). The owner-only "
            "response carries `verified_snapshot_total` + `retained_payments_"
            "total` diagnostics; those, and owner-entered `refunds[].reference`, "
            "are excluded from the cashier `OfflineSaleLookup` and from every "
            "audit-log row.\n"
            "* `LINKED_EXISTING_SALE` — fallback when the retained data cannot be "
            "verified; links an owner-entered COMPLETED sale in this branch. The "
            "backend compares three dimensions — (1) line items & quantities, "
            "(2) payment methods & amounts, (3) transaction total — against the "
            "retained record; any comparable dimension that mismatches -> "
            "`sale_incompatible`. `link_verification` reports `FULL` (all three "
            "comparable and matched), `PARTIAL` (every comparable one matched, "
            "fewer than three comparable) or `MANUAL_ATTESTED` (none comparable "
            "— needs `owner_attestation: true` + a ≥40-char `explanation`). "
            "Always `linked_manually: true`.\n\n"
            "Owner + MFA. Idempotent per record. A resolved record cannot be "
            "reconciled again. Stable codes: `offline_reconciliation_required`, "
            "`offline_record_not_reconcilable`, `offline_record_already_resolved`, "
            "`physical_count_required`, `count_variant_missing`, "
            "`count_variant_unexpected`, `count_variant_duplicated`, "
            "`invalid_count`, `reference_required`, `payment_reference_index_invalid`, "
            "`payment_reference_duplicated`, `payment_reference_unexpected`, "
            "`payment_mismatch`, `refund_total_mismatch`, `refund_required`, "
            "`invalid_refund_amount`, `no_payment_to_refund`, "
            "`offline_total_unverifiable`, `attested_total_required`, "
            "`attestation_explanation_too_short`, "
            "`partial_reconciliation_unsupported`, `offline_data_untrusted`, "
            "`sale_reference_required`, `sale_already_linked`, `sale_incompatible`, "
            "`manual_verification_required`, `not_found`."
        ),
        tags=["Offline"],
    )
    @action(detail=True, methods=["post"])
    def reconcile(self, request, pk=None):
        record = self.get_object()
        serializer = OfflineReconcileRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reconciliation = offline_reconciliation.reconcile_sync_record(
                record=record,
                owner=request.user,
                request=request,
                **serializer.validated_data,
            )
        except offline_service._NotFound as exc:
            raise NotFound("No such sale.") from exc
        record.refresh_from_db()
        return Response(
            OfflineReconciliationResultSerializer(
                {"sync_record": record, "reconciliation": reconciliation}
            ).data
        )


class OfflineSaleLookupView(APIView):
    permission_classes = [IsAuthenticatedAndMFAVerified]

    @extend_schema(
        responses={200: OfflineSaleLookupSerializer},
        summary="Look up one offline sale: outcome, resolution state and Sale mapping",
        description=(
            "The bound cashier's device polls this to reconcile a queued sale "
            "after the owner acts. Readable only by the cashier of the "
            "authorization that submitted this `client_sale_id` (the same "
            "binding as `POST /offline/sync/`); any other user, or an unknown "
            "id, gets an indistinguishable `404`.\n\n"
            "Returned for a record in **any** outcome — not only once an "
            "official Sale exists — so the device can clear a held row when the "
            "owner resolves a `CONFLICT` without creating a Sale, or resolves a "
            "`REJECTED` (which never creates one): `resolved` / `resolved_at` / "
            "`resolution_note` flip while `outcome` stays `CONFLICT` / "
            "`REJECTED` and `sale_id` stays `null`."
        ),
        tags=["Offline"],
    )
    def get(self, request, client_sale_id):
        record = (
            OfflineSaleSyncRecord.objects.filter(
                branch_id=request.user.branch_id, client_sale_id=client_sale_id
            )
            .select_related("authorization", "sale", "reconciliation")
            .first()
        )
        if record is None or record.authorization.cashier_id != request.user.id:
            raise NotFound("Unknown client sale reference.")
        reconciliation = getattr(record, "reconciliation", None)
        return Response(
            OfflineSaleLookupSerializer(
                {
                    "client_sale_id": record.client_sale_id,
                    "device_sequence": record.device_sequence,
                    "outcome": record.outcome,
                    "detail_code": record.detail_code,
                    "sale_id": record.sale_id,
                    "receipt_number": (
                        record.sale.receipt_number if record.sale_id else None
                    ),
                    "resolved": record.resolved,
                    "resolved_at": record.resolved_at,
                    "resolution_note": record.resolution_note,
                    "resolution_kind": (
                        reconciliation.kind if reconciliation is not None else None
                    ),
                }
            ).data
        )
