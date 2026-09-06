"""Offline fixed-price checkout: authorization lifecycle, exclusive session and
batch synchronisation.

Design:

* The **authorization** (``accounts.OfflineDeviceAuthorization``) binds one
  ``RegisteredDevice`` + branch + cashier + a versioned, signed catalogue
  snapshot + a validity window of at most
  ``settings.OFFLINE_AUTHORIZATION_MAX_HOURS``. Only one is ACTIVE per branch —
  that is the exclusive offline session.
* Offline sales are created on the device. The device later submits them in a
  device-bound batch. The server **recomputes every price and total from the
  authorised signed snapshot** — client-supplied prices, totals, discounts and
  costs are never trusted.
* Every submitted sale gets exactly one outcome — ACCEPTED, DUPLICATE, CONFLICT,
  REJECTED or OWNER_REVIEW_REQUIRED — recorded in ``OfflineSaleSyncRecord``.
  ``(branch, client_sale_id)`` is unique, so retries are idempotent and a
  stock conflict is retained for the owner, never discarded.
* Each sale syncs atomically; one bad sale never corrupts the rest of the batch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import (
    DeviceStatus,
    OfflineAuthorizationStatus,
    OfflineDeviceAuthorization,
)
from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money
from apps.core.services.audit import record_audit
from apps.sales.models import (
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    Sale,
    SaleSource,
    SaleStatus,
)
from apps.sales.services.offline_snapshot import (
    SnapshotVerificationError,
    build_catalogue_snapshot,
    sign_authorization_package,
    verify_authorization_token,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

_VALID_METHODS = {"CASH", "TRANSFER", "POS"}
_REFERENCE_METHODS = {"TRANSFER", "POS"}

# Outcomes that keep a sync record in the owner's review queue until it is
# resolved. REJECTED is included: a rejected offline sale is a real sale the
# device already took money for, so the owner must reconcile it (and can then
# mark the record resolved).
_REVIEW_QUEUE_OUTCOMES = (
    OfflineSyncOutcome.CONFLICT,
    OfflineSyncOutcome.OWNER_REVIEW_REQUIRED,
    OfflineSyncOutcome.REJECTED,
)
# A small tolerance on the *start* of the window only — device and server
# clocks are never perfectly aligned at session start. The 24h upper bound is
# not relaxed. See the SECURITY note about device-clock limitations.
_WINDOW_START_GRACE = timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# Inputs                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class OfflinePaymentInput:
    method: str
    amount: Decimal
    tendered_amount: Decimal | None = None
    reference: str = ""


@dataclass
class OfflineSaleInput:
    client_sale_id: object
    device_sequence: int
    offline_created_at: object  # datetime or ISO string
    items: list  # [{"variant_id": ..., "quantity": int}]
    payments: list  # [OfflinePaymentInput]
    customer_name: str = ""
    customer_phone: str = ""


@dataclass
class SyncResult:
    client_sale_id: str
    device_sequence: int
    outcome: str
    detail_code: str = ""
    sale_id: str | None = None
    receipt_number: str | None = None


class _SaleRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _SaleConflict(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- #
# Authorization lifecycle                                                     #
# --------------------------------------------------------------------------- #


def _validate_actors(*, branch, device, cashier, owner):
    if not (getattr(owner, "is_owner", False) and owner.branch_id == branch.id):
        raise APIError(
            "Only this branch's owner can authorise the offline device.",
            code="not_branch_owner",
            status_code=403,
        )
    if device.branch_id != branch.id or device.status != DeviceStatus.ACTIVE:
        raise APIError(
            "The device is not an active device for this branch.",
            code="invalid_offline_device",
        )
    if not cashier.is_active or cashier.branch_id != branch.id:
        raise APIError(
            "The cashier is not an active user of this branch.",
            code="invalid_offline_cashier",
        )


@transaction.atomic
def issue_offline_authorization(
    *, branch, device, cashier, owner, request=None
) -> OfflineDeviceAuthorization:
    _validate_actors(branch=branch, device=device, cashier=cashier, owner=owner)

    if (
        OfflineDeviceAuthorization.objects.select_for_update()
        .filter(branch=branch, status=OfflineAuthorizationStatus.ACTIVE)
        .exists()
    ):
        raise Conflict(
            "This branch already has an active offline session.",
            code="offline_session_active",
        )

    now = timezone.now()
    expires_at = now + timedelta(hours=int(settings.OFFLINE_AUTHORIZATION_MAX_HOURS))
    version = OfflineDeviceAuthorization.objects.filter(branch=branch).count() + 1
    snapshot = build_catalogue_snapshot(branch=branch, version=version)

    authorization = OfflineDeviceAuthorization.objects.create(
        branch=branch,
        device=device,
        cashier=cashier,
        authorized_by=owner,
        snapshot_version=version,
        snapshot=snapshot,
        signed_token="",
        issued_at=now,
        expires_at=expires_at,
        status=OfflineAuthorizationStatus.ACTIVE,
    )
    token = sign_authorization_package(
        snapshot=snapshot,
        branch_id=branch.id,
        device_id=device.id,
        authorization_id=authorization.id,
        cashier_id=cashier.id,
        issued_at=now,
        expires_at=expires_at,
    )
    authorization.signed_token = token
    authorization.save(update_fields=["signed_token", "updated_at"])

    record_audit(
        action="offline.authorize",
        target=authorization,
        actor=owner,
        branch=branch,
        request=request,
        after={
            "device": str(device.id),
            "cashier": str(cashier.id),
            "snapshot_version": version,
            "expires_at": expires_at.isoformat(),
            "variant_count": len(snapshot["variants"]),
        },
    )
    return authorization


def _flag_pending_for_review(authorization, *, actor, request, detail):
    updated = OfflineSaleSyncRecord.objects.filter(
        authorization=authorization,
        outcome__in=_REVIEW_QUEUE_OUTCOMES,
        resolved=False,
    ).count()
    record_audit(
        action="offline.session_close",
        target=authorization,
        actor=actor,
        branch=authorization.branch,
        request=request,
        after={"reason": detail, "unresolved_records": updated},
    )
    return updated


@transaction.atomic
def revoke_offline_authorization(
    *, authorization, owner, reason: str = "", request=None
) -> OfflineDeviceAuthorization:
    locked = OfflineDeviceAuthorization.objects.select_for_update().get(
        pk=authorization.pk
    )
    if locked.status in {
        OfflineAuthorizationStatus.REVOKED,
        OfflineAuthorizationStatus.REPLACED,
    }:
        raise Conflict(
            f"This authorization is already {locked.status.lower()}.",
            code="offline_authorization_closed",
        )
    locked.status = OfflineAuthorizationStatus.REVOKED
    locked.ended_at = timezone.now()
    locked.ended_by = owner
    locked.end_reason = (reason or "")[:300]
    locked.save(
        update_fields=["status", "ended_at", "ended_by", "end_reason", "updated_at"]
    )
    _flag_pending_for_review(locked, actor=owner, request=request, detail="revoked")
    record_audit(
        action="offline.revoke",
        target=locked,
        actor=owner,
        branch=locked.branch,
        request=request,
        after={"reason": (reason or "")[:120]},
    )
    return locked


@transaction.atomic
def replace_offline_authorization(
    *, authorization, owner, device=None, cashier=None, request=None
) -> OfflineDeviceAuthorization:
    locked = OfflineDeviceAuthorization.objects.select_for_update().get(
        pk=authorization.pk
    )
    if locked.status != OfflineAuthorizationStatus.ACTIVE:
        raise Conflict(
            "Only an active authorization can be replaced.",
            code="offline_authorization_not_active",
        )
    locked.status = OfflineAuthorizationStatus.REPLACED
    locked.ended_at = timezone.now()
    locked.ended_by = owner
    locked.end_reason = "replaced"
    locked.save(
        update_fields=["status", "ended_at", "ended_by", "end_reason", "updated_at"]
    )
    _flag_pending_for_review(locked, actor=owner, request=request, detail="replaced")
    return issue_offline_authorization(
        branch=locked.branch,
        device=device or locked.device,
        cashier=cashier or locked.cashier,
        owner=owner,
        request=request,
    )


def pending_review_count(authorization) -> int:
    return OfflineSaleSyncRecord.objects.filter(
        authorization=authorization,
        outcome__in=_REVIEW_QUEUE_OUTCOMES,
        resolved=False,
    ).count()


@transaction.atomic
def end_offline_session(
    *, authorization, owner, force: bool = False, reason: str = "", request=None
) -> OfflineDeviceAuthorization:
    locked = OfflineDeviceAuthorization.objects.select_for_update().get(
        pk=authorization.pk
    )
    if locked.status != OfflineAuthorizationStatus.ACTIVE:
        raise Conflict(
            "This offline session is not active.",
            code="offline_session_not_active",
        )

    outstanding = pending_review_count(locked)
    if outstanding and not force:
        raise Conflict(
            "Unresolved offline sales remain; resolve them or force-end the session.",
            code="unresolved_offline_sales",
        )

    locked.status = (
        OfflineAuthorizationStatus.FORCE_CLOSED
        if (force and outstanding)
        else OfflineAuthorizationStatus.CLOSED
    )
    locked.ended_at = timezone.now()
    locked.ended_by = owner
    locked.end_reason = (reason or ("force-ended" if force else "ended"))[:300]
    locked.save(
        update_fields=["status", "ended_at", "ended_by", "end_reason", "updated_at"]
    )
    record_audit(
        action="offline.session_force_end" if force else "offline.session_end",
        target=locked,
        actor=owner,
        branch=locked.branch,
        request=request,
        after={"forced": bool(force), "unresolved_records": outstanding},
    )
    return locked


# --------------------------------------------------------------------------- #
# Token verification + status                                                 #
# --------------------------------------------------------------------------- #


def load_authorization_for_token(
    *, token: str, branch, user
) -> tuple[OfflineDeviceAuthorization, dict]:
    """Verify a signed token and load its authorization, checking the binding.

    Cross-branch / cross-device / wrong-cashier access is reported as *not
    found* (404) by callers; a bad signature is a hard 400.
    """

    try:
        decoded = verify_authorization_token(token)
    except SnapshotVerificationError as exc:
        raise APIError(
            "The offline authorization could not be verified.",
            code="invalid_signature",
        ) from exc

    authorization = (
        OfflineDeviceAuthorization.objects.select_related("device", "cashier")
        .filter(pk=decoded.get("authorization_id"))
        .first()
    )
    if authorization is None:
        raise _NotFound()
    if (
        str(authorization.branch_id) != str(decoded.get("branch_id"))
        or str(authorization.device_id) != str(decoded.get("device_id"))
        or authorization.branch_id != branch.id
        or authorization.cashier_id != user.id
        or authorization.signed_token != token
    ):
        raise _NotFound()
    return authorization, decoded["snapshot"]


class _NotFound(Exception):
    """Internal: surfaced by views as a 404."""


def authorization_status(authorization) -> dict:
    return {
        "authorization_id": str(authorization.id),
        "status": authorization.status,
        "expires_at": authorization.expires_at.isoformat(),
        "is_expired": authorization.is_expired,
        "snapshot_version": authorization.snapshot_version,
        "synced_count": OfflineSaleSyncRecord.objects.filter(
            authorization=authorization, outcome=OfflineSyncOutcome.ACCEPTED
        ).count(),
        "pending_review_count": pending_review_count(authorization),
    }


# --------------------------------------------------------------------------- #
# Catalogue-snapshot retrieval (no writes)                                    #
# --------------------------------------------------------------------------- #


def catalogue_snapshot_for_cashier(*, authorization, user) -> dict:
    """Project the frozen, signed fixed-price catalogue for the bound cashier.

    Retrieval only. The snapshot returned is exactly what was signed when the
    offline session opened — it is deliberately **not** rebuilt here, so a later
    online price or stock change can never leak into a running offline session,
    and repeated reads are identical.

    Identity is bound exactly as ``POST /offline/sync/`` binds it: the stored
    signed token is verified (constant-time via ``django.core.signing``) and its
    branch / device / cashier must match the caller. Any mismatch — wrong
    cashier, wrong device, wrong branch, or a device that is no longer the
    active registered device — is surfaced as *not found* so it cannot reveal
    whether another snapshot exists. A session the cashier legitimately owns
    that is expired / revoked / replaced / force-closed returns the standard
    safe error envelope instead.

    The returned payload carries no cost, profit, stock value, credential,
    signing key or customer data; only the signed token (already returned by
    ``POST /offline/authorizations/``) is echoed back as the proof the sync
    protocol requires.
    """

    # Same verification + binding as the sync path. Raises ``_NotFound`` on a
    # branch / device / cashier / token mismatch and
    # ``APIError(code="invalid_signature")`` if the stored token fails the HMAC
    # check — neither is weakened here.
    _bound, snapshot = load_authorization_for_token(
        token=authorization.signed_token,
        branch=authorization.branch,
        user=user,
    )

    if authorization.device.status != DeviceStatus.ACTIVE:
        raise _NotFound()

    if authorization.status != OfflineAuthorizationStatus.ACTIVE:
        raise Conflict(
            "This offline session is no longer active.",
            code="offline_session_not_active",
        )
    if authorization.is_expired:
        raise Conflict(
            "This offline authorization has expired.",
            code="offline_authorization_expired",
        )

    return {
        "authorization_id": str(authorization.id),
        "status": authorization.status,
        "snapshot_version": authorization.snapshot_version,
        "issued_at": authorization.issued_at,
        "expires_at": authorization.expires_at,
        "signed_token": authorization.signed_token,
        "items": snapshot["variants"],
    }


# --------------------------------------------------------------------------- #
# Temporary receipt (no writes)                                              #
# --------------------------------------------------------------------------- #


def temporary_receipt(*, sale_input: OfflineSaleInput, snapshot: dict) -> dict:
    """Build the labelled offline receipt context from the signed snapshot.

    No official receipt number — that is assigned only after synchronisation.
    """

    price_by_id = {row["variant_id"]: row for row in snapshot["variants"]}
    lines = []
    total = ZERO
    for item in sale_input.items:
        row = price_by_id.get(str(item["variant_id"]))
        if row is None:
            raise APIError(
                "An item is not in the authorised catalogue.",
                code="price_not_in_snapshot",
            )
        qty = int(item["quantity"])
        unit_price = to_money(row["price"])
        line_total = to_money(unit_price * qty)
        total += line_total
        lines.append(
            {
                "sku": row["sku"],
                "name": row["name"],
                "quantity": qty,
                "unit_price": str(unit_price),
                "line_total": str(line_total),
            }
        )
    payments = [
        {
            "method": p.method,
            "amount": str(to_money(p.amount)),
            "confirmed_by_cashier": True,
            "electronically_verified": False,
        }
        for p in sale_input.payments
    ]
    return {
        "label": "OFFLINE RECEIPT — PENDING SYNCHRONIZATION",
        "official_receipt_number": None,
        "client_reference": str(sale_input.client_sale_id).split("-")[0].upper(),
        "offline_datetime": _as_datetime(sale_input.offline_created_at).isoformat(),
        "cashier_note": (
            "The official receipt number is assigned after this sale "
            "synchronises with the server."
        ),
        "items": lines,
        "total": str(to_money(total)),
        "payments": payments,
    }


# --------------------------------------------------------------------------- #
# Batch synchronisation                                                       #
# --------------------------------------------------------------------------- #


def _as_datetime(value):
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise _SaleRejected("invalid_timestamp")
        value = parsed
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _redacted(sale_input: OfflineSaleInput) -> dict:
    return {
        "device_sequence": int(sale_input.device_sequence),
        "customer_name": (sale_input.customer_name or "")[:150],
        "items": [
            {"variant_id": str(i["variant_id"]), "quantity": int(i["quantity"])}
            for i in sale_input.items
        ],
        "payments": [
            {
                "method": p.method,
                "amount": str(to_money(p.amount)),
                "has_reference": bool((p.reference or "").strip()),
            }
            for p in sale_input.payments
        ],
    }


def _validate_offline_payments(sale_input: OfflineSaleInput, total: Decimal) -> None:
    if not sale_input.payments:
        raise _SaleRejected("payment_required")
    running = ZERO
    for pay in sale_input.payments:
        if pay.method not in _VALID_METHODS:
            raise _SaleRejected("invalid_payment_method")
        try:
            amount = to_money(pay.amount)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise _SaleRejected("invalid_payment_amount") from exc
        if amount <= 0:
            raise _SaleRejected("invalid_payment_amount")
        if pay.method in _REFERENCE_METHODS and not (pay.reference or "").strip():
            raise _SaleRejected("reference_required")
        if pay.tendered_amount is not None:
            if pay.method != "CASH":
                raise _SaleRejected("invalid_tendered_amount")
            if to_money(pay.tendered_amount) < amount:
                raise _SaleRejected("invalid_tendered_amount")
        running += amount
    if running != total:
        raise _SaleRejected("payment_mismatch")


def _recompute_total(sale_input, price_by_id) -> Decimal:
    if not sale_input.items:
        raise _SaleRejected("empty_cart")
    total = ZERO
    for item in sale_input.items:
        row = price_by_id.get(str(item["variant_id"]))
        if row is None:
            raise _SaleRejected("price_not_in_snapshot")
        try:
            qty = int(item["quantity"])
        except (TypeError, ValueError) as exc:
            raise _SaleRejected("invalid_quantity") from exc
        if qty <= 0 or qty != float(item["quantity"]):
            raise _SaleRejected("invalid_quantity")
        total += to_money(Decimal(row["price"]) * qty)
    return to_money(total)


@transaction.atomic
def sync_offline_batch(
    *, branch, authorization, snapshot, sales: list[OfflineSaleInput], request=None
) -> list[SyncResult]:
    max_batch = int(getattr(settings, "OFFLINE_SYNC_MAX_BATCH", 200))
    if len(sales) > max_batch:
        raise APIError(
            f"A sync batch may carry at most {max_batch} sales.",
            code="batch_too_large",
            status_code=413,
        )

    price_by_id = {row["variant_id"]: row for row in snapshot["variants"]}
    # The session is only allowed to sync straight through while it is ACTIVE
    # and still inside its window. Anything else — ended / force-ended / revoked
    # / replaced, or an ACTIVE token whose window has lapsed — holds every sale
    # in the batch for the owner to review.
    review_mode = authorization.needs_owner_review or authorization.is_expired
    ordered = sorted(sales, key=lambda s: int(s.device_sequence))

    # Duplicate device sequences inside the batch (different client ids).
    seen_seq: dict[int, str] = {}
    dup_seq_ids: set[str] = set()
    for item in ordered:
        seq = int(item.device_sequence)
        cid = str(item.client_sale_id)
        if seq in seen_seq and seen_seq[seq] != cid:
            dup_seq_ids.add(cid)
        seen_seq.setdefault(seq, cid)

    results: list[SyncResult] = []
    counts: dict[str, int] = {}
    for sale_input in ordered:
        result = _sync_one(
            branch=branch,
            authorization=authorization,
            snapshot_price_by_id=price_by_id,
            sale_input=sale_input,
            review_mode=review_mode,
            duplicate_sequence=str(sale_input.client_sale_id) in dup_seq_ids,
            request=request,
        )
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        results.append(result)

    record_audit(
        action="offline.sync_batch",
        target=authorization,
        actor=getattr(request, "user", None),
        branch=branch,
        request=request,
        after={"submitted": len(ordered), "outcomes": counts},
    )
    return results


def _existing_result(record: OfflineSaleSyncRecord) -> SyncResult:
    outcome = record.outcome
    if outcome == OfflineSyncOutcome.ACCEPTED:
        outcome = OfflineSyncOutcome.DUPLICATE
    return SyncResult(
        client_sale_id=str(record.client_sale_id),
        device_sequence=record.device_sequence,
        outcome=outcome,
        detail_code=record.detail_code,
        sale_id=str(record.sale_id) if record.sale_id else None,
        receipt_number=(record.sale.receipt_number if record.sale_id else None),
    )


def _record_outcome(
    *,
    branch,
    authorization,
    sale_input,
    outcome,
    detail_code="",
    sale=None,
    request=None,
    audit=False,
) -> OfflineSaleSyncRecord:
    try:
        with transaction.atomic():
            record = OfflineSaleSyncRecord.objects.create(
                branch=branch,
                authorization=authorization,
                device=authorization.device,
                client_sale_id=sale_input.client_sale_id,
                device_sequence=int(sale_input.device_sequence),
                offline_created_at=_as_datetime(sale_input.offline_created_at),
                outcome=outcome,
                detail_code=detail_code,
                redacted_payload=_redacted(sale_input),
                sale=sale,
            )
    except IntegrityError:
        record = OfflineSaleSyncRecord.objects.get(
            branch=branch, client_sale_id=sale_input.client_sale_id
        )
    if audit and outcome in {
        OfflineSyncOutcome.CONFLICT,
        OfflineSyncOutcome.REJECTED,
        OfflineSyncOutcome.OWNER_REVIEW_REQUIRED,
    }:
        record_audit(
            action=f"offline.sync_{outcome.lower()}",
            target=record,
            actor=getattr(request, "user", None),
            branch=branch,
            request=request,
            after={"detail": detail_code, "sequence": record.device_sequence},
        )
    return record


def _sync_one(
    *,
    branch,
    authorization,
    snapshot_price_by_id,
    sale_input: OfflineSaleInput,
    review_mode: bool,
    duplicate_sequence: bool,
    request=None,
) -> SyncResult:
    # Idempotency: an outcome already exists for this client_sale_id.
    existing = OfflineSaleSyncRecord.objects.filter(
        branch=branch, client_sale_id=sale_input.client_sale_id
    ).first()
    if existing is not None:
        return _existing_result(existing)

    # A completed Sale with no record (defensive backstop).
    prior_sale = Sale.objects.filter(
        branch=branch, client_sale_id=sale_input.client_sale_id
    ).first()
    if prior_sale is not None and prior_sale.status == SaleStatus.COMPLETED:
        record = _record_outcome(
            branch=branch,
            authorization=authorization,
            sale_input=sale_input,
            outcome=OfflineSyncOutcome.ACCEPTED,
            sale=prior_sale,
        )
        return _existing_result(record)

    if review_mode:
        # A non-ACTIVE status names itself (closed / force_closed / revoked /
        # replaced); an ACTIVE-but-lapsed window is reported as "expired".
        detail_code = (
            authorization.status.lower()
            if authorization.status != OfflineAuthorizationStatus.ACTIVE
            else "expired"
        )
        record = _record_outcome(
            branch=branch,
            authorization=authorization,
            sale_input=sale_input,
            outcome=OfflineSyncOutcome.OWNER_REVIEW_REQUIRED,
            detail_code=detail_code,
            request=request,
            audit=True,
        )
        return _existing_result(record)

    if duplicate_sequence:
        record = _record_outcome(
            branch=branch,
            authorization=authorization,
            sale_input=sale_input,
            outcome=OfflineSyncOutcome.REJECTED,
            detail_code="duplicate_sequence",
            request=request,
            audit=True,
        )
        return _existing_result(record)

    try:
        created_at = _as_datetime(sale_input.offline_created_at)
        window_start = authorization.issued_at - _WINDOW_START_GRACE
        if not (window_start <= created_at <= authorization.expires_at):
            raise _SaleRejected("outside_window")

        total = _recompute_total(sale_input, snapshot_price_by_id)
        _validate_offline_payments(sale_input, total)

        with transaction.atomic():
            sale = _create_offline_sale(
                branch=branch,
                authorization=authorization,
                sale_input=sale_input,
                snapshot_price_by_id=snapshot_price_by_id,
                created_at=created_at,
                request=request,
            )
            record = OfflineSaleSyncRecord.objects.create(
                branch=branch,
                authorization=authorization,
                device=authorization.device,
                client_sale_id=sale_input.client_sale_id,
                device_sequence=int(sale_input.device_sequence),
                offline_created_at=created_at,
                outcome=OfflineSyncOutcome.ACCEPTED,
                redacted_payload=_redacted(sale_input),
                sale=sale,
            )
        return SyncResult(
            client_sale_id=str(sale_input.client_sale_id),
            device_sequence=int(sale_input.device_sequence),
            outcome=OfflineSyncOutcome.ACCEPTED,
            sale_id=str(sale.id),
            receipt_number=sale.receipt_number,
        )
    except _SaleRejected as exc:
        record = _record_outcome(
            branch=branch,
            authorization=authorization,
            sale_input=sale_input,
            outcome=OfflineSyncOutcome.REJECTED,
            detail_code=exc.code,
            request=request,
            audit=True,
        )
        return _existing_result(record)
    except _SaleConflict as exc:
        record = _record_outcome(
            branch=branch,
            authorization=authorization,
            sale_input=sale_input,
            outcome=OfflineSyncOutcome.CONFLICT,
            detail_code=exc.code,
            request=request,
            audit=True,
        )
        return _existing_result(record)
    except IntegrityError:
        # Lost a concurrent race for the same client_sale_id.
        record = OfflineSaleSyncRecord.objects.filter(
            branch=branch, client_sale_id=sale_input.client_sale_id
        ).first()
        if record is not None:
            return _existing_result(record)
        raise


def _create_offline_sale(
    *, branch, authorization, sale_input, snapshot_price_by_id, created_at, request
) -> Sale:
    fixed_prices = {vid: row["price"] for vid, row in snapshot_price_by_id.items()}
    cart = [
        CartLine(
            variant_id=uuid.UUID(str(i["variant_id"])),
            quantity=int(i["quantity"]),
        )
        for i in sale_input.items
    ]
    payments = [
        PaymentLine(
            method=p.method,
            amount=to_money(p.amount),
            tendered_amount=(
                to_money(p.tendered_amount) if p.tendered_amount is not None else None
            ),
            reference=(p.reference or "").strip(),
        )
        for p in sale_input.payments
    ]
    customer = None
    name = (sale_input.customer_name or "").strip()
    phone = (sale_input.customer_phone or "").strip()
    if name or phone:
        from apps.sales.models import Customer

        customer = Customer.objects.create(branch=branch, name=name, phone=phone)

    try:
        return create_sale(
            branch=branch,
            cashier=authorization.cashier,
            cart=cart,
            payments=payments,
            client_sale_id=sale_input.client_sale_id,
            customer=customer,
            source=SaleSource.OFFLINE,
            fixed_prices=fixed_prices,
            completed_at=created_at,
            business_date=timezone.localtime(created_at).date(),
            payments_confirmed_offline=True,
            request=request,
        )
    # Conflict is a subclass of APIError — check it first.
    except Conflict as exc:
        if getattr(exc, "code", "") == "sale_in_progress":
            # A concurrent worker created this exact sale; treat as a retry.
            raise _SaleConflict("sale_in_progress") from exc
        raise
    except APIError as exc:
        code = getattr(exc, "code", "")
        if code == "stock_not_available":
            raise _SaleConflict("stock_not_available") from exc
        if code in {
            "variant_not_found",
            "variant_unavailable",
            "price_not_in_snapshot",
        }:
            raise _SaleRejected(code) from exc
        raise


# --------------------------------------------------------------------------- #
# Owner resolution of retained records                                        #
# --------------------------------------------------------------------------- #


@transaction.atomic
def resolve_sync_record(
    *, record, owner, note: str, request=None
) -> OfflineSaleSyncRecord:
    """DEPRECATED note-only path — kept as a guard.

    A ``CONFLICT`` / ``REJECTED`` / ``OWNER_REVIEW_REQUIRED`` record affects
    stock, revenue, payments and COGS, none of which a note can put right, so it
    can no longer be closed here — it must go through
    :func:`apps.sales.services.offline_reconciliation.reconcile_sync_record`.
    ``ACCEPTED`` / ``DUPLICATE`` records were never resolvable. Historical
    already-``resolved`` rows are untouched.
    """

    locked = OfflineSaleSyncRecord.objects.select_for_update().get(pk=record.pk)
    if locked.outcome in _REVIEW_QUEUE_OUTCOMES:
        raise Conflict(
            "This record affects stock or money and must be reconciled, not "
            "noted. Use POST /api/v1/offline/sync-records/{id}/reconcile/.",
            code="offline_reconciliation_required",
        )
    raise Conflict(
        "Only a CONFLICT, REJECTED or OWNER_REVIEW_REQUIRED record can be "
        "resolved, and those must be reconciled.",
        code="offline_record_not_resolvable",
    )
