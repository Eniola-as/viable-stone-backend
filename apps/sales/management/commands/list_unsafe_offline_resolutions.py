"""List (and optionally reopen) offline sync records that the deprecated
note-only resolver closed **without** a structured reconciliation.

An unsafe historical resolution is a record where:

* ``outcome`` is CONFLICT / REJECTED / OWNER_REVIEW_REQUIRED, and
* ``resolved = True``, and
* there is **no** ``OfflineSaleReconciliation`` row.

For such a record stock, revenue, payments and COGS were never put right. This
command surfaces them for human review before deployment; it **never** invents
a Sale, refund or stock movement. ``--reopen`` clears the ``resolved`` flag
(and audits the change) so the record re-enters the review queue and can be
reconciled through the proper endpoint.

No customer or payment information is printed — only ids, the branch code, the
outcome / detail code, timestamps, the resolver's username and whether a Sale
already exists for the same ``client_sale_id``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.services.audit import record_audit
from apps.sales.models import (
    OfflineSaleSyncRecord,
    OfflineSyncOutcome,
    Sale,
)

_ACCOUNTING_OUTCOMES = [
    OfflineSyncOutcome.CONFLICT,
    OfflineSyncOutcome.REJECTED,
    OfflineSyncOutcome.OWNER_REVIEW_REQUIRED,
]


def unsafe_offline_resolutions():
    """Queryset of accounting-impacting records resolved with no reconciliation."""

    return (
        OfflineSaleSyncRecord.objects.filter(
            outcome__in=_ACCOUNTING_OUTCOMES, resolved=True
        )
        .filter(reconciliation__isnull=True)
        .select_related("branch", "resolved_by")
        .order_by("branch__code", "resolved_at")
    )


class Command(BaseCommand):
    help = (
        "List offline sync records the deprecated note-only resolver closed "
        "without a structured reconciliation. Use --reopen to return them to "
        "the review queue (no financial or stock activity is created)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reopen",
            action="store_true",
            help="Set resolved=False on the listed records (audited).",
        )

    def handle(self, *args, **options):
        records = list(unsafe_offline_resolutions())
        self.stdout.write(f"Unsafe historical offline resolutions: {len(records)}")
        if not records:
            return

        self.stdout.write(
            "id  branch  outcome  detail_code  resolved_at  resolved_by  "
            "official_sale_exists"
        )
        for r in records:
            has_sale = Sale.objects.filter(
                branch_id=r.branch_id, client_sale_id=r.client_sale_id
            ).exists()
            self.stdout.write(
                f"{r.id}  {r.branch.code}  {r.outcome}  {r.detail_code or '-'}  "
                f"{r.resolved_at.isoformat() if r.resolved_at else '-'}  "
                f"{getattr(r.resolved_by, 'username', '-')}  {has_sale}"
            )

        if not options["reopen"]:
            self.stdout.write(
                "\nReview each with GET /api/v1/offline/sync-records/{id}/ then "
                "reconcile it (POST .../reconcile/). Re-run with --reopen to "
                "return them to the queue."
            )
            return

        now = timezone.now()
        for r in records:
            r.resolved = False
            r.resolved_by = None
            r.resolved_at = None
            r.save(
                update_fields=["resolved", "resolved_by", "resolved_at", "updated_at"]
            )
            record_audit(
                action="offline.reopen_unsafe_resolution",
                target=r,
                branch=r.branch,
                after={
                    "outcome": r.outcome,
                    "detail_code": r.detail_code,
                    "previous_resolution_note": (r.resolution_note or "")[:120],
                    "reopened_at": now.isoformat(),
                },
            )
        self.stdout.write(
            f"\nReopened {len(records)} record(s); each is back in the review "
            "queue and must be reconciled."
        )
