"""G3 — GET /api/v1/offline/sync-records/ gains validated ?outcome / ?resolved.

The handoff's owner conflict queue is documented as
``?outcome=CONFLICT`` / ``?resolved=false``. The viewset already applied those
params ad-hoc; this locks them as documented, validated filters that compose
with pagination / ordering / search and reject bad input with the standard
validation envelope. Owner-only + branch scoping unchanged.
"""

import uuid

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import EmployeeFactory
from apps.sales.models import OfflineSaleSyncRecord, OfflineSyncOutcome
from apps.sales.services.offline import issue_offline_authorization

pytestmark = pytest.mark.django_db

BASE = "/api/v1/offline/sync-records/"


@pytest.fixture
def records(branch, owner):
    from apps.accounts.tests.factories import RegisteredDeviceFactory

    auth = issue_offline_authorization(
        branch=branch,
        device=RegisteredDeviceFactory(branch=branch),
        cashier=EmployeeFactory(branch=branch),
        owner=owner,
    )

    def _rec(outcome, resolved=False, seq=1):
        return OfflineSaleSyncRecord.objects.create(
            branch=branch,
            authorization=auth,
            device=auth.device,
            client_sale_id=uuid.uuid4(),
            device_sequence=seq,
            offline_created_at=timezone.now(),
            outcome=outcome,
            resolved=resolved,
        )

    return {
        "conflict_open": _rec(OfflineSyncOutcome.CONFLICT, resolved=False, seq=1),
        "conflict_done": _rec(OfflineSyncOutcome.CONFLICT, resolved=True, seq=2),
        "review_open": _rec(
            OfflineSyncOutcome.OWNER_REVIEW_REQUIRED, resolved=False, seq=3
        ),
        "accepted": _rec(OfflineSyncOutcome.ACCEPTED, resolved=False, seq=4),
    }


class TestOutcomeFilter:
    def test_outcome_conflict(self, login_as, owner, records):
        body = login_as(owner).get(f"{BASE}?outcome=CONFLICT").json()
        assert {r["id"] for r in body["results"]} == {
            str(records["conflict_open"].id),
            str(records["conflict_done"].id),
        }

    def test_resolved_false(self, login_as, owner, records):
        body = login_as(owner).get(f"{BASE}?resolved=false").json()
        ids = {r["id"] for r in body["results"]}
        assert str(records["conflict_done"].id) not in ids
        assert str(records["conflict_open"].id) in ids

    def test_combined_outcome_and_resolved(self, login_as, owner, records):
        body = login_as(owner).get(f"{BASE}?outcome=CONFLICT&resolved=false").json()
        assert {r["id"] for r in body["results"]} == {str(records["conflict_open"].id)}

    def test_composes_with_pagination(self, login_as, owner, records):
        res = login_as(owner).get(f"{BASE}?resolved=false&page_size=1")
        assert res.status_code == 200
        assert res.json()["count"] == 3  # conflict_open, review_open, accepted


class TestInvalidValues:
    def test_invalid_outcome_is_validation_error(self, login_as, owner, records):
        res = login_as(owner).get(f"{BASE}?outcome=NOPE")
        assert res.status_code == 400
        body = res.json()
        assert body["code"] == "validation_error"
        assert "outcome" in body["field_errors"]

    def test_invalid_resolved_is_validation_error(self, login_as, owner, records):
        res = login_as(owner).get(f"{BASE}?resolved=perhaps")
        assert res.status_code == 400
        assert res.json()["code"] == "validation_error"


class TestScoping:
    def test_employee_is_forbidden(self, login_as, branch, owner, records):
        emp = EmployeeFactory(branch=branch)
        assert login_as(emp).get(f"{BASE}?outcome=CONFLICT").status_code == 403


class TestOpenAPI:
    def test_documents_outcome_and_resolved(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        params = {
            p["name"]
            for p in schema["paths"]["/api/v1/offline/sync-records/"]["get"][
                "parameters"
            ]
        }
        assert {"outcome", "resolved"} <= params
