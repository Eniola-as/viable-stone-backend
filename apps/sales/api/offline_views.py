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
from rest_framework import mixins, status, viewsets
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
from apps.sales.api.offline_serializers import (
    OfflineAuthorizationCreateSerializer,
    OfflineAuthorizationReplaceSerializer,
    OfflineAuthorizationRevokeSerializer,
    OfflineAuthorizationSerializer,
    OfflineAuthorizationStatusSerializer,
    OfflineDeviceCreateSerializer,
    OfflineDeviceSerializer,
    OfflineSessionEndSerializer,
    OfflineSyncRecordResolveSerializer,
    OfflineSyncRecordSerializer,
    OfflineSyncRequestSerializer,
    OfflineSyncResultSerializer,
    OfflineTemporaryReceiptRequestSerializer,
)
from apps.sales.models import OfflineSaleSyncRecord, Sale
from apps.sales.services import offline as offline_service
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
        responses={200: OfflineSyncResultSerializer(many=True)},
        summary="Synchronise a batch of offline sales (device-bound)",
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
            {
                "results": OfflineSyncResultSerializer(
                    [r.__dict__ for r in results], many=True
                ).data
            }
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
    retrieve=extend_schema(summary="Retrieve an offline sync record", tags=["Offline"]),
)
class OfflineSyncRecordViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = OfflineSyncRecordSerializer
    permission_classes = [IsOwner]
    queryset = OfflineSaleSyncRecord.objects.none()

    def get_queryset(self):
        queryset = OfflineSaleSyncRecord.objects.filter(
            branch_id=self.request.user.branch_id
        ).select_related("sale")
        outcome = self.request.query_params.get("outcome")
        if outcome:
            queryset = queryset.filter(outcome=outcome)
        resolved = self.request.query_params.get("resolved")
        if resolved in {"true", "false"}:
            queryset = queryset.filter(resolved=resolved == "true")
        return queryset

    @extend_schema(
        request=OfflineSyncRecordResolveSerializer,
        responses={200: OfflineSyncRecordSerializer},
        summary="Resolve a retained conflict / review record (owner)",
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


class OfflineSaleLookupView(APIView):
    permission_classes = [IsAuthenticatedAndMFAVerified]

    @extend_schema(
        responses={200: dict},
        summary="Map a client_sale_id to its official Sale + receipt number",
        tags=["Offline"],
    )
    def get(self, request, client_sale_id):
        branch_id = request.user.branch_id
        record = (
            OfflineSaleSyncRecord.objects.filter(
                branch_id=branch_id, client_sale_id=client_sale_id
            )
            .select_related("sale")
            .first()
        )
        sale = Sale.objects.filter(
            branch_id=branch_id, client_sale_id=client_sale_id
        ).first()
        if record is None and sale is None:
            raise NotFound("Unknown client sale reference.")
        official = record.sale if (record and record.sale_id) else sale
        return Response(
            {
                "client_sale_id": str(client_sale_id),
                "synced": official is not None,
                "outcome": record.outcome if record else None,
                "sale_id": str(official.id) if official else None,
                "official_receipt_number": (
                    official.receipt_number if official else None
                ),
            }
        )
