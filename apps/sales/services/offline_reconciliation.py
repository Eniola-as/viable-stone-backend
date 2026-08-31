"""Safe owner reconciliation of accounting-impacting offline sync records.

A ``CONFLICT`` / ``REJECTED`` / ``OWNER_REVIEW_REQUIRED`` sync record means **no
Sale, stock movement, payment, revenue or COGS exists yet**. A bare note must
never close one. This module is the only safe path: the owner picks one of

* ``RECORDED_AS_SALE``       — the customer kept the goods and the shop kept the
  payment. The backend re-reads the retained payload + the *verified* signed
  snapshot and creates the official Sale itself (authoritative prices, COGS and
  receipt), with a physical-count correction where stock has since drifted.
* ``REFUNDED_AND_RETURNED``  — the customer returned **everything** and was
  refunded in **full**. No Sale / Payment / SaleReturn / stock movement; only
  immutable refund evidence. The refund must equal the money **actually
  collected** from the customer, which is *not* assumed to be the catalogue
  total: it is trusted only when the retained device payments parsed cleanly
  and equal the verified snapshot total, otherwise the owner must attest it.
* ``LINKED_EXISTING_SALE``   — fallback only, when the retained data cannot be
  trusted and the owner has manually re-entered the sale from paper evidence.

Every path is one ``transaction.atomic()``; the sync-record row is
``select_for_update``-locked so two owners cannot both win, and the
reconciliation row is 1:1 so a retry reuses the same result.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.core.exceptions import APIError, Conflict
from apps.core.money import ZERO, to_money
from apps.core.services.audit import record_audit
from apps.inventory.models import InventoryBalance, MovementType
from apps.inventory.services.stock import lock_balances, write_movement
from apps.sales.models import (
    OfflineAmountSource,
    OfflineLinkVerification,
    OfflineReconciliationCount,
    OfflineReconciliationKind,
    OfflineReconciliationRefund,
    OfflineSaleReconciliation,
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    Sale,
    SaleSource,
    SaleStatus,
)
from apps.sales.services.offline import (
    _REVIEW_QUEUE_OUTCOMES,
    _WINDOW_START_GRACE,
    SnapshotVerificationError,
    _NotFound,
    verify_authorization_token,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

_REFERENCE_METHODS = {"TRANSFER", "POS"}
_RECONCILABLE = set(
    _REVIEW_QUEUE_OUTCOMES
)  # CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED

_OFFLINE_RECONCILIATION_REF = "offline_reconciliation"


class _Untrusted(APIError):
    def __init__(self, message: str):
        super().__init__(message, code="offline_data_untrusted")


# --------------------------------------------------------------------------- #
# Retained-data verification                                                  #
# --------------------------------------------------------------------------- #


def verify_retained_authorization(record: OfflineSaleSyncRecord) -> dict:
    """Return the frozen catalogue snapshot iff the retained signed token still
    verifies **and** its branch / device / cashier / authorization binding
    matches the record. Raise ``offline_data_untrusted`` otherwise.
    """

    auth = record.authorization
    token = auth.signed_token or ""
    try:
        decoded = verify_authorization_token(token)
    except SnapshotVerificationError as exc:
        raise _Untrusted(
            "The retained offline authorization could not be verified."
        ) from exc
    if (
        str(decoded.get("authorization_id")) != str(auth.id)
        or str(decoded.get("branch_id")) != str(record.branch_id)
        or str(decoded.get("device_id")) != str(record.device_id)
        or str(decoded.get("cashier_id")) != str(auth.cashier_id)
        or "snapshot" not in decoded
    ):
        raise _Untrusted(
            "The retained offline authorization binding does not match the record."
        )
    return decoded["snapshot"]


def _affected_quantities(record: OfflineSaleSyncRecord) -> dict[str, int]:
    payload = record.redacted_payload or {}
    items = payload.get("items") or []
    if not items:
        raise _Untrusted("The retained offline data has no items.")
    out: dict[str, int] = {}
    for it in items:
        try:
            vid = str(it["variant_id"])
            qty = int(it["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _Untrusted("The retained offline items are malformed.") from exc
        if qty <= 0:
            raise _Untrusted("A retained offline item has a non-positive quantity.")
        out[vid] = out.get(vid, 0) + qty
    return out


def _verified_offline_total(record: OfflineSaleSyncRecord) -> Decimal | None:
    """The full offline transaction amount, recomputed from the
    **cryptographically verified** frozen snapshot — or ``None`` when the
    retained data cannot be trusted enough to establish it.

    ``None`` when: the signed token fails verification or its branch / device /
    cashier / authorization binding does not match; the retained items are
    malformed; or an item is not in the verified snapshot (e.g. a
    ``price_not_in_snapshot`` / ``variant_not_found`` REJECTED record). A
    ``payment_mismatch`` REJECTED record still yields a verified total — the
    snapshot prices are authoritative regardless of what the device recorded as
    paid.
    """

    try:
        snapshot = verify_retained_authorization(record)
        affected = _affected_quantities(record)
    except APIError:
        return None
    price_by_id = {r["variant_id"]: r for r in snapshot["variants"]}
    if not all(vid in price_by_id for vid in affected):
        return None
    return to_money(
        sum(
            (Decimal(price_by_id[vid]["price"]) * qty for vid, qty in affected.items()),
            ZERO,
        )
    )


def _retained_payments(record: OfflineSaleSyncRecord) -> list[dict] | None:
    """The retained payment lines as ``[{payment_index, method, amount,
    reference_required}]`` (0-based ``payment_index`` = position in the stored
    payload). ``None`` if the payload has no usable payments.

    The device's original Transfer/POS **reference** was never stored (Stage 16
    redaction) — only ``method`` and ``amount`` are retained.
    """

    raw = (record.redacted_payload or {}).get("payments") or []
    out: list[dict] = []
    for i, p in enumerate(raw):
        try:
            method = str(p["method"])
            amount = to_money(p["amount"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None
        out.append(
            {
                "payment_index": i,
                "method": method,
                "amount": amount,
                "reference_required": method in _REFERENCE_METHODS,
            }
        )
    return out or None


def _retained_payments_total(record: OfflineSaleSyncRecord) -> Decimal | None:
    """Sum of the retained device payment amounts, or ``None`` when the retained
    payments are absent or unparseable. This is *what the device recorded as
    taken* — it is trusted as the collected amount only when it agrees with the
    verified snapshot total (see ``_resolve_collected_amount``).
    """

    rp = _retained_payments(record)
    if rp is None:
        return None
    return to_money(sum((p["amount"] for p in rp), ZERO))


def _completion_time(record: OfflineSaleSyncRecord):
    auth = record.authorization
    t = record.offline_created_at
    if t and (auth.issued_at - _WINDOW_START_GRACE) <= t <= auth.expires_at:
        return t, False
    return timezone.now(), True


def _reconciliation_customer(record: OfflineSaleSyncRecord):
    name = ((record.redacted_payload or {}).get("customer_name") or "").strip()
    if not name:
        return None
    from apps.sales.models import Customer

    return Customer.objects.create(branch=record.branch, name=name, phone="")


# --------------------------------------------------------------------------- #
# Count validation                                                            #
# --------------------------------------------------------------------------- #


def _validate_counts(counts_in, *, expected_vids: set[str]) -> dict[str, int]:
    by_vid: dict[str, int] = {}
    for c in counts_in or []:
        vid = str(c["variant"])
        raw = c["counted_on_hand"]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise APIError(
                "Each counted_on_hand must be a whole number of zero or more.",
                code="invalid_count",
                field_errors={"counts": ["whole number >= 0"]},
            )
        if vid in by_vid:
            raise APIError(
                "A variant was counted more than once.",
                code="count_variant_duplicated",
            )
        by_vid[vid] = raw
    unexpected = set(by_vid) - expected_vids
    if unexpected:
        raise APIError(
            "A counted variant is not part of this offline sale.",
            code="count_variant_unexpected",
        )
    missing = expected_vids - set(by_vid)
    if missing:
        raise APIError(
            "Every affected variant must be counted exactly once.",
            code="count_variant_missing",
            field_errors={"counts": [f"missing: {sorted(missing)}"]},
        )
    return by_vid


# --------------------------------------------------------------------------- #
# Reconciliation row                                                          #
# --------------------------------------------------------------------------- #


def _make_reconciliation(
    *,
    locked,
    owner,
    kind,
    explanation,
    now,
    sale=None,
    receipt_number="",
    refund_total=None,
    linked_manually=False,
    completion_time_substituted=False,
    amount_source="",
    link_verification="",
    verified_snapshot_total=None,
    retained_payments_total=None,
) -> OfflineSaleReconciliation:
    try:
        with transaction.atomic():
            return OfflineSaleReconciliation.objects.create(
                sync_record=locked,
                branch=locked.branch,
                kind=kind,
                resolved_by=owner,
                resolved_at=now,
                explanation=explanation,
                sale=sale,
                receipt_number=receipt_number or "",
                refund_total=refund_total,
                linked_manually=linked_manually,
                completion_time_substituted=completion_time_substituted,
                amount_source=amount_source,
                link_verification=link_verification,
                verified_snapshot_total=verified_snapshot_total,
                retained_payments_total=retained_payments_total,
            )
    except IntegrityError:
        # Lost a concurrent race for this sync record.
        return OfflineSaleReconciliation.objects.get(sync_record=locked)


# --------------------------------------------------------------------------- #
# RECORDED_AS_SALE                                                            #
# --------------------------------------------------------------------------- #


def _recorded_as_sale(*, locked, owner, now, body, request):
    snapshot = verify_retained_authorization(locked)
    price_by_id = {r["variant_id"]: r for r in snapshot["variants"]}
    affected = _affected_quantities(locked)
    if not all(vid in price_by_id for vid in affected):
        raise _Untrusted("An offline item is no longer in the authorised snapshot.")

    retained = _retained_payments(locked)
    if not retained:
        raise _Untrusted("The retained offline data has no usable payments.")

    # Structured association: {payment_index -> reference}. ``payment_index`` is
    # the 0-based position in the retained payment list (see the read model's
    # ``retained_payments``), so a split Transfer/POS batch is never mis-mapped.
    supplied: dict[int, str] = {}
    for entry in body.get("payment_references") or []:
        idx = int(entry["payment_index"])
        ref = str(entry.get("reference") or "").strip()
        if idx < 0 or idx >= len(retained):
            raise APIError(
                f"payment_index {idx} is not a retained payment "
                f"(0..{len(retained) - 1}).",
                code="payment_reference_index_invalid",
                field_errors={"payment_references": ["payment_index out of range"]},
            )
        if idx in supplied:
            raise APIError(
                f"payment_index {idx} was given a reference twice.",
                code="payment_reference_duplicated",
                field_errors={"payment_references": ["one entry per payment_index"]},
            )
        supplied[idx] = ref

    payment_lines: list[PaymentLine] = []
    for rp in retained:
        reference = supplied.get(rp["payment_index"], "")
        if rp["reference_required"] and not reference:
            raise APIError(
                "Supply the reference for every Transfer / POS payment "
                "(payment_references: [{payment_index, reference}]).",
                code="reference_required",
                field_errors={
                    "payment_references": [
                        f"reference required for payment_index "
                        f"{rp['payment_index']} ({rp['method']} {rp['amount']})"
                    ]
                },
            )
        if reference and not rp["reference_required"]:
            raise APIError(
                f"payment_index {rp['payment_index']} is a {rp['method']} payment "
                "and takes no reference.",
                code="payment_reference_unexpected",
                field_errors={"payment_references": ["no reference for a CASH line"]},
            )
        payment_lines.append(
            PaymentLine(method=rp["method"], amount=rp["amount"], reference=reference)
        )

    total = to_money(
        sum(
            (Decimal(price_by_id[vid]["price"]) * qty for vid, qty in affected.items()),
            ZERO,
        )
    )
    running = to_money(sum((pl.amount for pl in payment_lines), ZERO))
    if running != total:
        raise APIError(
            f"The retained payments total {running}; the sale total is {total}. "
            "Reconcile with LINKED_EXISTING_SALE from trusted evidence, or "
            "REFUNDED_AND_RETURNED if the whole transaction was reversed.",
            code="payment_mismatch",
        )

    variant_objs = {
        str(v.id): v
        for v in ProductVariant.objects.filter(
            id__in=list(affected), product__branch=locked.branch
        )
    }
    if len(variant_objs) != len(affected):
        raise _Untrusted("An offline item no longer exists for this branch.")

    balances = lock_balances(locked.branch, [uuid.UUID(vid) for vid in affected])

    counts_in = body.get("counts")
    stock_conflict = locked.outcome == OfflineSyncOutcome.CONFLICT
    count_rows: list[tuple] = []

    if stock_conflict or counts_in is not None:
        if counts_in is None:
            raise Conflict(
                "Physically count every affected variant before reconciling a "
                "stock-conflict record.",
                code="physical_count_required",
            )
        counted = _validate_counts(counts_in, expected_vids=set(affected))
        for vid, qty in affected.items():
            bal = balances[uuid.UUID(vid)]
            db_before = bal.quantity
            pre_sale = counted[vid] + qty
            delta = pre_sale - db_before
            if delta != 0:
                write_movement(
                    balance=bal,
                    delta=delta,
                    movement_type=MovementType.OFFLINE_RECONCILIATION,
                    created_by=owner,
                    reference_type=_OFFLINE_RECONCILIATION_REF,
                    reference_id=locked.id,
                    reason=(
                        f"OFFLINE_RECONCILIATION count={counted[vid]} "
                        f"sale_qty={qty} client_sale_id={locked.client_sale_id}"
                    ),
                )
                bal.refresh_from_db()
            count_rows.append((variant_objs[vid], counted[vid], qty, db_before, delta))
    else:
        for vid, qty in affected.items():
            if balances[uuid.UUID(vid)].quantity < qty:
                raise Conflict(
                    "Stock is insufficient; physically count the affected "
                    "variants and resubmit with counts.",
                    code="physical_count_required",
                )

    completed_at, substituted = _completion_time(locked)
    cart = [
        CartLine(variant_id=uuid.UUID(vid), quantity=qty)
        for vid, qty in affected.items()
    ]
    fixed_prices = {vid: price_by_id[vid]["price"] for vid in affected}
    try:
        sale = create_sale(
            branch=locked.branch,
            cashier=locked.authorization.cashier,
            cart=cart,
            payments=payment_lines,
            client_sale_id=locked.client_sale_id,
            customer=_reconciliation_customer(locked),
            source=SaleSource.OFFLINE,
            fixed_prices=fixed_prices,
            completed_at=completed_at,
            business_date=timezone.localtime(completed_at).date(),
            payments_confirmed_offline=True,
            request=request,
        )
    except Conflict:
        raise
    except APIError as exc:
        code = getattr(exc, "code", "")
        if code == "stock_not_available":
            raise Conflict(
                "Stock is still insufficient after the count; re-count and resubmit.",
                code="physical_count_required",
            ) from exc
        if code in {
            "variant_not_found",
            "variant_unavailable",
            "price_not_in_snapshot",
        }:
            raise _Untrusted(
                "The retained offline data cannot be turned into a sale."
            ) from exc
        raise

    for variant, counted_qty, _q, _b, _d in count_rows:
        bal = InventoryBalance.objects.get(branch=locked.branch, variant=variant)
        if bal.quantity != counted_qty or bal.quantity < 0:
            raise APIError(
                "Reconciliation did not land on the counted quantity.",
                code="reconciliation_failed",
                status_code=500,
            )

    recon = _make_reconciliation(
        locked=locked,
        owner=owner,
        kind=OfflineReconciliationKind.RECORDED_AS_SALE,
        explanation=body["explanation"],
        now=now,
        sale=sale,
        receipt_number=sale.receipt_number,
        completion_time_substituted=substituted,
        # RECORDED_AS_SALE only proceeds past verify_retained_authorization, so
        # the total that the Sale was built from is always snapshot-verified.
        amount_source=OfflineAmountSource.SNAPSHOT_VERIFIED,
    )
    if recon.counts.exists():  # a concurrent winner already filled these
        return recon
    for variant, counted_qty, qty, db_before, delta in count_rows:
        OfflineReconciliationCount.objects.create(
            reconciliation=recon,
            variant=variant,
            counted_on_hand=counted_qty,
            quantity_in_offline_sale=qty,
            db_quantity_before=db_before,
            correction_delta=delta,
        )
    return recon


# --------------------------------------------------------------------------- #
# REFUNDED_AND_RETURNED                                                       #
# --------------------------------------------------------------------------- #


def _resolve_collected_amount(locked, body) -> tuple[Decimal, str, dict]:
    """The money **actually collected** from the customer (the exact figure the
    refund evidence must total) + how it was established + owner-only
    diagnostics.

    * Trusted automatically **only** when the retained device payments parse
      cleanly and their sum equals the cryptographically verified snapshot total
      (``RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT``). The catalogue total on its
      own is never used as the refund amount, and a device payment total that
      disagrees with the snapshot is never trusted on its own either.
    * Otherwise the request MUST carry ``owner_attestation: true`` +
      ``attested_offline_total`` (the amount the owner confirms was collected,
      from physical evidence) + an explanation of >= 40 chars; the result is
      flagged ``OWNER_ATTESTED``.
    * If the established amount is not positive the record cannot be a refund
      (``no_payment_to_refund``).

    ``diagnostics`` carries ``verified_snapshot_total`` and
    ``retained_payments_total`` (either may be ``None``) for the owner-only
    response — never for cashiers or audit rows.
    """

    snapshot_total = _verified_offline_total(locked)
    retained_total = _retained_payments_total(locked)
    diagnostics = {
        "verified_snapshot_total": snapshot_total,
        "retained_payments_total": retained_total,
    }

    if (
        snapshot_total is not None
        and retained_total is not None
        and retained_total == snapshot_total
    ):
        if snapshot_total <= ZERO:
            raise Conflict(
                "No money was collected on this offline sale, so it cannot be "
                "reconciled as a refund.",
                code="no_payment_to_refund",
            )
        return (
            snapshot_total,
            OfflineAmountSource.RETAINED_PAYMENTS_MATCHED_TO_SNAPSHOT,
            diagnostics,
        )

    if not body.get("owner_attestation"):
        raise Conflict(
            "The amount collected from the customer cannot be established from "
            "trusted retained data (the retained device payments are missing / "
            "unverifiable, or disagree with the verified catalogue total). "
            "Resubmit with owner_attestation: true and attested_offline_total = "
            "the amount actually collected, plus a >= 40 character explanation.",
            code="offline_total_unverifiable",
        )
    attested = body.get("attested_offline_total")
    if attested is None:
        raise APIError(
            "attested_offline_total is required when owner_attestation is true.",
            code="attested_total_required",
            field_errors={"attested_offline_total": ["required"]},
        )
    attested = to_money(attested)
    if attested <= ZERO:
        raise Conflict(
            "attested_offline_total must be the positive amount actually "
            "collected from the customer. A REFUNDED_AND_RETURNED needs money to "
            "have changed hands; if nothing was collected this record cannot be "
            "reconciled as a refund.",
            code="no_payment_to_refund",
        )
    if len((body.get("explanation") or "").strip()) < 40:
        raise APIError(
            "An owner-attested reconciliation needs a detailed explanation "
            "(at least 40 characters) of the physical evidence relied on.",
            code="attestation_explanation_too_short",
            field_errors={"explanation": ["at least 40 characters"]},
        )
    return attested, OfflineAmountSource.OWNER_ATTESTED, diagnostics


def _refunded_and_returned(*, locked, owner, now, body, request):
    if not (body.get("all_goods_returned") and body.get("full_amount_refunded")):
        raise Conflict(
            "Only a full return with a full refund can be reconciled without a "
            "sale. For a partial return, use RECORDED_AS_SALE and then the "
            "normal return workflow at POST /api/v1/sales/{id}/return-requests/.",
            code="partial_reconciliation_unsupported",
        )

    collected, amount_source, diagnostics = _resolve_collected_amount(locked, body)
    refunds = body.get("refunds") or []
    if not refunds:
        raise APIError(
            "At least one refund-evidence row is required.",
            code="refund_required",
            field_errors={"refunds": ["required"]},
        )

    running = ZERO
    clean: list[tuple] = []
    for r in refunds:
        method = r["method"]
        amount = to_money(r["amount"])
        if amount <= 0:
            raise APIError(
                "Every refund amount must be greater than zero.",
                code="invalid_refund_amount",
            )
        reference = (r.get("reference") or "").strip()
        if method in _REFERENCE_METHODS and not reference:
            raise APIError(
                "A reference is required for a Transfer / POS refund.",
                code="reference_required",
                field_errors={"refunds": ["reference required for TRANSFER/POS"]},
            )
        running += amount
        clean.append((method, amount, reference))
    if running != collected:
        raise APIError(
            f"Refund evidence totals {running}; it must total the amount "
            f"actually collected from the customer ({collected}, {amount_source}).",
            code="refund_total_mismatch",
            field_errors={"refunds": [f"must total {collected}"]},
        )

    recon = _make_reconciliation(
        locked=locked,
        owner=owner,
        kind=OfflineReconciliationKind.REFUNDED_AND_RETURNED,
        explanation=body["explanation"],
        now=now,
        refund_total=collected,
        amount_source=amount_source,
        verified_snapshot_total=diagnostics["verified_snapshot_total"],
        retained_payments_total=diagnostics["retained_payments_total"],
    )
    if recon.refunds.exists():
        return recon
    for method, amount, reference in clean:
        OfflineReconciliationRefund.objects.create(
            reconciliation=recon, method=method, amount=amount, reference=reference
        )
    return recon


# --------------------------------------------------------------------------- #
# LINKED_EXISTING_SALE (fallback)                                            #
# --------------------------------------------------------------------------- #


def _linked_existing_sale(*, locked, owner, now, body, request):
    sale_id = body.get("sale_id")
    receipt = (body.get("receipt_number") or "").strip()
    qs = Sale.objects.filter(
        branch_id=locked.branch_id, status=SaleStatus.COMPLETED
    ).prefetch_related("items", "payments")
    if sale_id:
        sale = qs.filter(pk=sale_id).first()
    elif receipt:
        sale = qs.filter(receipt_number=receipt).first()
    else:
        raise APIError(
            "Provide sale_id or receipt_number.",
            code="sale_reference_required",
            field_errors={"sale_id": ["sale_id or receipt_number is required"]},
        )
    if sale is None:
        raise _NotFound()  # cross-branch / missing -> 404 not_found (indistinguishable)

    # A COMPLETED offline sale already owns a sync record (1:1); an online sale
    # does not. Reject a target that any other record / reconciliation claims.
    if (
        OfflineSaleSyncRecord.objects.filter(sale=sale).exclude(pk=locked.pk).exists()
        or OfflineSaleReconciliation.objects.filter(sale=sale).exists()
    ):
        raise Conflict(
            "That sale is already linked to another offline sync record.",
            code="sale_already_linked",
        )

    # --- Compare the candidate against the retained record across three ----- #
    # dimensions: (1) line items and their quantities, (2) payment methods and
    # amounts, (3) the transaction total. ``checks_possible`` counts how many of
    # the three could be compared at all (the retained value was present /
    # derivable); ``checks_passed`` how many matched. FULL == all three
    # comparable and matched; PARTIAL == every comparable one matched but fewer
    # than three were comparable; MANUAL_ATTESTED == none comparable.
    sale_items = {str(si.variant_id): si.quantity for si in sale.items.all()}
    sale_payments = sorted(
        (str(p.method), to_money(p.amount)) for p in sale.payments.all()
    )

    checks_possible = 0
    checks_passed = 0

    # dimension 1 — items + quantities (from redacted_payload — the server's own
    # record; not signed, but a strong structural check even for a broken token)
    try:
        retained_items = _affected_quantities(locked)
    except APIError:
        retained_items = None
    if retained_items is not None:
        checks_possible += 1
        if sale_items != retained_items:
            raise Conflict(
                "The linked sale's items / quantities do not match the retained "
                "offline sale.",
                code="sale_incompatible",
            )
        checks_passed += 1

    # dimension 2 — payment methods + amounts (references were never retained)
    retained_pays = _retained_payments(locked)
    if retained_pays is not None:
        checks_possible += 1
        retained_pay_pairs = sorted(
            (rp["method"], rp["amount"]) for rp in retained_pays
        )
        if sale_payments != retained_pay_pairs:
            raise Conflict(
                "The linked sale's payment methods / amounts do not match the "
                "retained offline sale.",
                code="sale_incompatible",
            )
        checks_passed += 1

    # dimension 3 — transaction total (verified snapshot figure preferred; else
    # the retained payment sum)
    verified_total = _verified_offline_total(locked)
    total_ref = verified_total
    amount_source = ""
    if verified_total is not None:
        amount_source = OfflineAmountSource.SNAPSHOT_VERIFIED
    elif retained_pays is not None:
        total_ref = to_money(sum((rp["amount"] for rp in retained_pays), ZERO))
    if total_ref is not None:
        checks_possible += 1
        if sale.total != total_ref:
            raise Conflict(
                f"The linked sale's total ({sale.total}) does not match the "
                f"retained offline transaction total ({total_ref}).",
                code="sale_incompatible",
            )
        checks_passed += 1

    if checks_possible == 0:
        # Retained data too damaged to compare anything — require an explicit
        # owner attestation + MFA (enforced by the view) + detailed explanation.
        if not body.get("owner_attestation"):
            raise Conflict(
                "None of the retained items, payments or total could be "
                "compared against the sale. Resubmit with owner_attestation: "
                "true and a detailed explanation of how the match was verified "
                "by hand.",
                code="manual_verification_required",
            )
        if len((body.get("explanation") or "").strip()) < 40:
            raise APIError(
                "A manually-verified link needs a detailed explanation (at "
                "least 40 characters) of the physical evidence relied on.",
                code="attestation_explanation_too_short",
                field_errors={"explanation": ["at least 40 characters"]},
            )
        link_verification = OfflineLinkVerification.MANUAL_ATTESTED
        amount_source = OfflineAmountSource.OWNER_ATTESTED
    elif checks_possible == 3 and checks_passed == 3:
        link_verification = OfflineLinkVerification.FULL
    else:
        link_verification = OfflineLinkVerification.PARTIAL

    return _make_reconciliation(
        locked=locked,
        owner=owner,
        kind=OfflineReconciliationKind.LINKED_EXISTING_SALE,
        explanation=body["explanation"],
        now=now,
        sale=sale,
        receipt_number=sale.receipt_number,
        linked_manually=True,
        link_verification=link_verification,
        amount_source=amount_source,
    )


_DISPATCH = {
    OfflineReconciliationKind.RECORDED_AS_SALE: _recorded_as_sale,
    OfflineReconciliationKind.REFUNDED_AND_RETURNED: _refunded_and_returned,
    OfflineReconciliationKind.LINKED_EXISTING_SALE: _linked_existing_sale,
}


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


@transaction.atomic
def reconcile_sync_record(
    *, record: OfflineSaleSyncRecord, owner, request=None, **body
) -> OfflineSaleReconciliation:
    locked = (
        OfflineSaleSyncRecord.objects.select_for_update()
        .select_related("authorization", "branch")
        .get(pk=record.pk)
    )
    if locked.outcome not in _RECONCILABLE:
        raise Conflict(
            "Only a CONFLICT, REJECTED or OWNER_REVIEW_REQUIRED record is "
            "reconciled here.",
            code="offline_record_not_reconcilable",
        )

    kind = body["kind"]
    existing = OfflineSaleReconciliation.objects.filter(sync_record=locked).first()
    if existing is not None:
        if existing.kind == kind:
            return existing  # idempotent retry
        raise Conflict(
            "This record has already been reconciled and cannot be changed.",
            code="offline_record_already_resolved",
        )
    if locked.resolved:
        # a historical note-only resolution with no reconciliation row
        raise Conflict(
            "This record was already resolved.",
            code="offline_record_already_resolved",
        )

    now = timezone.now()
    recon = _DISPATCH[kind](
        locked=locked, owner=owner, now=now, body=body, request=request
    )

    locked.resolved = True
    locked.resolved_by = owner
    locked.resolved_at = now
    locked.resolution_note = f"{kind}: {body['explanation'][:400]}"
    update_fields = [
        "resolved",
        "resolved_by",
        "resolved_at",
        "resolution_note",
        "updated_at",
    ]
    if recon.sale_id and locked.sale_id != recon.sale_id:
        locked.sale = recon.sale
        update_fields.append("sale")
    locked.save(update_fields=update_fields)

    record_audit(
        action="offline.reconcile",
        target=locked,
        actor=owner,
        branch=locked.branch,
        request=request,
        after={
            "kind": kind,
            "sale": str(recon.sale_id) if recon.sale_id else "",
            "receipt_number": recon.receipt_number,
            "refund_total": str(recon.refund_total) if recon.refund_total else "",
            # categorical labels only — the owner-only diagnostic amounts
            # (verified_snapshot_total / retained_payments_total) and every
            # refund reference are deliberately kept out of the audit row.
            "amount_source": recon.amount_source,
            "link_verification": recon.link_verification,
            "completion_time_substituted": recon.completion_time_substituted,
            "explanation": body["explanation"][:200],
        },
    )
    return recon
