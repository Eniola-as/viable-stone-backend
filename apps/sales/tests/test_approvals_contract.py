"""G16 — the polymorphic contract of ``POST /api/v1/approvals/{id}/approve/``.

The endpoint dispatches on the approval's ``request_type``:

* ``DISCOUNT`` — body ``{amount, reviewer_note?}``  → responds with the updated
  ``ApprovalRequest``.
* ``RETURN``   — body ``{lines[], refunds[], reviewer_note?, client_return_id?}``
  → responds with the created ``SaleReturn``.

These tests pin that runtime behaviour *and* assert that ``openapi.yml``
represents both request bodies and both response bodies (a ``oneOf``), so a
frontend can dispatch on ``request_type`` instead of guessing.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales"
APPROVALS = "/api/v1/approvals"
SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "openapi.yml").read_text(encoding="utf-8")
)


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def pending_discount(db, branch, owner, login_as):
    cashier = EmployeeFactory(branch=branch)
    v1 = stocked_variant(
        branch, owner, price="1000.00", quantity=20, unit_cost="600.00"
    )
    c = login_as(cashier)
    sale_id = c.post(
        f"{SALES}/drafts/",
        {
            "client_sale_id": str(uuid.uuid4()),
            "items": [{"variant": str(v1.id), "quantity": 3}],
        },
        format="json",
    ).json()["id"]
    approval_id = c.post(
        f"{SALES}/{sale_id}/discount-requests/",
        {"amount": "500.00", "reason": "trade customer"},
        format="json",
    ).json()["id"]
    return {"approval_id": approval_id, "sale_id": sale_id, "cashier": cashier}


@pytest.fixture
def pending_return(db, branch, owner, login_as):
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(
        branch, owner, price="1000.00", quantity=15, unit_cost="600.00"
    )
    sale = create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=5)],
        payments=[
            PaymentLine(
                method="CASH",
                amount=Decimal("5000.00"),
                tendered_amount=Decimal("5000.00"),
            )
        ],
        client_sale_id=uuid.uuid4(),
    )
    item = sale.items.get()
    approval_id = (
        login_as(cashier)
        .post(
            f"{SALES}/{sale.id}/return-requests/",
            {
                "reason": "wrong colour supplied",
                "client_return_id": str(uuid.uuid4()),
                "lines": [{"sale_item": str(item.id), "quantity": 2}],
            },
            format="json",
        )
        .json()["id"]
    )
    return {"approval_id": approval_id, "item_id": str(item.id), "cashier": cashier}


def _return_body(item_id):
    return {
        "lines": [{"sale_item": item_id, "quantity": 2, "condition": "RESELLABLE"}],
        "refunds": [{"method": "CASH", "amount": "2000.00"}],
    }


# --------------------------------------------------------------------------- #
# runtime contract — DISCOUNT                                                 #
# --------------------------------------------------------------------------- #


class TestDiscountApproveRuntime:
    def test_discount_approve_takes_amount_and_returns_the_approval_request(
        self, login_as, owner, pending_discount
    ):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_discount['approval_id']}/approve/",
            {"amount": "500.00", "reviewer_note": "ok"},
            format="json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        # ApprovalRequest shape — carries request_type, NOT refunds/items
        assert body["request_type"] == "DISCOUNT"
        assert body["status"] == "APPROVED"
        assert "refunds" not in body and "items" not in body

    def test_discount_approve_requires_amount(self, login_as, owner, pending_discount):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_discount['approval_id']}/approve/", {}, format="json"
        )
        assert res.status_code == 400
        assert "amount" in res.json()["field_errors"]

    def test_discount_approve_rejects_a_return_body_with_a_clear_error(
        self, login_as, owner, pending_discount
    ):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_discount['approval_id']}/approve/",
            _return_body(str(uuid.uuid4())),  # lines + refunds, no amount
            format="json",
        )
        assert res.status_code == 400
        assert "amount" in res.json()["field_errors"]  # names the missing field


# --------------------------------------------------------------------------- #
# runtime contract — RETURN                                                   #
# --------------------------------------------------------------------------- #


class TestReturnApproveRuntime:
    def test_return_approve_takes_lines_and_refunds_and_returns_a_sale_return(
        self, login_as, owner, pending_return
    ):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_return['approval_id']}/approve/",
            _return_body(pending_return["item_id"]),
            format="json",
        )
        assert res.status_code == 200, res.content
        body = res.json()
        # SaleReturnRead shape — refunds + items + total, NO request_type
        assert set({"refunds", "items", "total", "client_return_id"}) <= set(body)
        assert "request_type" not in body
        assert body["total"] == "2000.00"
        assert len(body["refunds"]) == 1

    def test_return_approve_rejects_a_discount_body(
        self, login_as, owner, pending_return
    ):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_return['approval_id']}/approve/",
            {"amount": "500.00"},
            format="json",
        )
        assert res.status_code == 400
        errs = res.json()["field_errors"]
        assert "lines" in errs and "refunds" in errs

    def test_return_approve_needs_refunds(self, login_as, owner, pending_return):
        res = login_as(owner).post(
            f"{APPROVALS}/{pending_return['approval_id']}/approve/",
            {
                "lines": [
                    {
                        "sale_item": pending_return["item_id"],
                        "quantity": 2,
                        "condition": "RESELLABLE",
                    }
                ]
            },
            format="json",
        )
        assert res.status_code == 400
        assert "refunds" in res.json()["field_errors"]


# --------------------------------------------------------------------------- #
# dispatch field, authz, MFA, branch isolation                                #
# --------------------------------------------------------------------------- #


class TestApprovalDispatchAndAuthz:
    def test_list_and_detail_expose_request_type_for_dispatch(
        self, login_as, owner, pending_discount, pending_return
    ):
        listing = login_as(owner).get(f"{APPROVALS}/").json()["results"]
        types = {row["request_type"] for row in listing}
        assert {"DISCOUNT", "RETURN"} <= types
        detail = (
            login_as(owner)
            .get(f"{APPROVALS}/{pending_discount['approval_id']}/")
            .json()
        )
        assert detail["request_type"] == "DISCOUNT"
        assert "status" in detail

    def test_employee_cannot_approve_either_type(
        self, login_as, owner, pending_discount, pending_return
    ):
        emp = EmployeeFactory(branch=pending_discount["cashier"].branch)
        assert (
            login_as(emp)
            .post(
                f"{APPROVALS}/{pending_discount['approval_id']}/approve/",
                {"amount": "500.00"},
                format="json",
            )
            .status_code
            == 403
        )
        assert (
            login_as(emp)
            .post(
                f"{APPROVALS}/{pending_return['approval_id']}/approve/",
                _return_body(pending_return["item_id"]),
                format="json",
            )
            .status_code
            == 403
        )

    def test_owner_without_mfa_cannot_approve(self, login_as, owner, pending_discount):
        res = login_as(owner, mfa_verified=False).post(
            f"{APPROVALS}/{pending_discount['approval_id']}/approve/",
            {"amount": "500.00"},
            format="json",
        )
        assert res.status_code == 403

    def test_cross_branch_approval_id_is_404(self, login_as, pending_discount):
        outsider = OwnerFactory(branch=BranchFactory(code="VS71"))
        res = login_as(outsider).post(
            f"{APPROVALS}/{pending_discount['approval_id']}/approve/",
            {"amount": "500.00"},
            format="json",
        )
        assert res.status_code == 404


# --------------------------------------------------------------------------- #
# OpenAPI contract — the request AND response must be polymorphic             #
# --------------------------------------------------------------------------- #


class TestApproveOpenApiContract:
    @property
    def _approve(self):
        return SPEC["paths"]["/api/v1/approvals/{id}/approve/"]["post"]

    def _schema(self, ref: str) -> dict:
        return SPEC["components"]["schemas"][ref.split("/")[-1]]

    def _variants(self, node: dict) -> list[dict]:
        """Resolve a schema node to the list of concrete option schemas."""
        if "$ref" in node:
            node = self._schema(node["$ref"])
        options = node.get("oneOf") or node.get("anyOf")
        if not options:
            return [node]
        return [self._schema(o["$ref"]) if "$ref" in o else o for o in options]

    def test_request_body_is_a_oneof_of_discount_and_return(self):
        body = self._approve["requestBody"]["content"]["application/json"]["schema"]
        options = self._variants(body)
        blobs = [json.dumps(o) for o in options]
        # a discount option: has `amount`, no `lines`
        assert any(
            "amount" in o.get("properties", {})
            and "lines" not in o.get("properties", {})
            for o in options
        ), blobs
        # a return option: has `lines` and `refunds`
        assert any(
            {"lines", "refunds"} <= set(o.get("properties", {})) for o in options
        ), blobs

    def test_success_response_is_a_oneof_of_sale_return_and_approval_request(self):
        schema = self._approve["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        names = set()
        node = schema
        if "$ref" in node:
            names.add(node["$ref"].split("/")[-1])
            node = self._schema(node["$ref"])
        for opt in node.get("oneOf", []) or node.get("anyOf", []):
            if "$ref" in opt:
                names.add(opt["$ref"].split("/")[-1])
        assert {"SaleReturnRead", "ApprovalRequest"} <= names, names

    def test_approval_request_schema_exposes_request_type_enum(self):
        prop = self._schema("ApprovalRequest")["properties"]["request_type"]
        ref = prop.get("$ref") or prop["allOf"][0]["$ref"]
        assert {"DISCOUNT", "RETURN"} <= set(self._schema(ref)["enum"])
