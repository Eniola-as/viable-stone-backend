"""Stage 14 — owner-only protected stock adjustments."""

import uuid
from decimal import Decimal

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.catalog.tests.factories import ProductFactory, ProductVariantFactory
from apps.core.exceptions import APIError
from apps.core.models import AuditLog
from apps.inventory.models import InventoryBalance, MovementType, StockMovement
from apps.inventory.services.adjustments import adjust_stock
from apps.inventory.services.stock import open_stock

pytestmark = pytest.mark.django_db

ADJUST = "/api/v1/inventory/adjustments/"


def _variant(branch, owner, qty):
    v = ProductVariantFactory(product=ProductFactory(branch=branch, is_active=True))
    if qty:
        open_stock(
            branch=branch,
            variant=v,
            quantity=qty,
            unit_cost=Decimal("100"),
            created_by=owner,
        )
    return v


class TestAdjustStockService:
    def test_increase_creates_one_immutable_movement_and_audit(self, branch, owner):
        v = _variant(branch, owner, 10)
        adjust_stock(
            branch=branch,
            variant=v,
            direction="INCREASE",
            quantity=4,
            reason="Recount after supplier delivery discrepancy resolved",
            actor=owner,
            client_adjustment_id=uuid.uuid4(),
        )
        bal = InventoryBalance.objects.get(branch=branch, variant=v)
        assert bal.quantity == 14
        mv = StockMovement.objects.get(variant=v, movement_type=MovementType.ADJUSTMENT)
        assert mv.quantity_delta == 4
        assert "supplier delivery" in mv.reason
        entry = AuditLog.objects.get(action="stock.adjust", target_id=v.id)
        assert entry.before["quantity"] == 10
        assert entry.after["quantity"] == 14
        assert entry.after["change"] == 4

    def test_decrease_cannot_make_stock_negative(self, branch, owner):
        v = _variant(branch, owner, 3)
        with pytest.raises(APIError) as exc:
            adjust_stock(
                branch=branch,
                variant=v,
                direction="DECREASE",
                quantity=5,
                reason="Write-off of water-damaged stock from roof leak",
                actor=owner,
                client_adjustment_id=uuid.uuid4(),
            )
        assert exc.value.code == "stock_not_available"
        assert InventoryBalance.objects.get(branch=branch, variant=v).quantity == 3

    def test_reason_must_be_detailed(self, branch, owner):
        v = _variant(branch, owner, 5)
        with pytest.raises(APIError):
            adjust_stock(
                branch=branch,
                variant=v,
                direction="INCREASE",
                quantity=1,
                reason="fix",
                actor=owner,
                client_adjustment_id=uuid.uuid4(),
            )

    def test_quantity_must_be_positive_whole(self, branch, owner):
        v = _variant(branch, owner, 5)
        with pytest.raises(APIError):
            adjust_stock(
                branch=branch,
                variant=v,
                direction="INCREASE",
                quantity=0,
                reason="A perfectly detailed reason string here",
                actor=owner,
                client_adjustment_id=uuid.uuid4(),
            )

    def test_invalid_direction_rejected(self, branch, owner):
        v = _variant(branch, owner, 5)
        with pytest.raises(APIError):
            adjust_stock(
                branch=branch,
                variant=v,
                direction="SIDEWAYS",
                quantity=1,
                reason="A perfectly detailed reason string here",
                actor=owner,
                client_adjustment_id=uuid.uuid4(),
            )

    def test_idempotency_key_prevents_duplicate(self, branch, owner):
        v = _variant(branch, owner, 10)
        key = uuid.uuid4()
        kw = {
            "branch": branch,
            "variant": v,
            "direction": "INCREASE",
            "quantity": 4,
            "reason": "Correcting an earlier miscount of tinting bases",
            "actor": owner,
            "client_adjustment_id": key,
        }
        adjust_stock(**kw)
        adjust_stock(**kw)  # retry
        assert InventoryBalance.objects.get(branch=branch, variant=v).quantity == 14
        assert (
            StockMovement.objects.filter(
                variant=v, movement_type=MovementType.ADJUSTMENT
            ).count()
            == 1
        )


class TestAdjustStockApi:
    def test_employee_gets_403(self, login_as, branch):
        emp = EmployeeFactory(branch=branch)
        res = login_as(emp).post(
            ADJUST,
            {
                "variant": str(uuid.uuid4()),
                "direction": "INCREASE",
                "quantity": 1,
                "reason": "A perfectly detailed reason string here",
                "client_adjustment_id": str(uuid.uuid4()),
            },
            format="json",
        )
        assert res.status_code == 403

    def test_owner_adjusts_via_api(self, login_as, owner, branch):
        v = _variant(branch, owner, 8)
        res = login_as(owner).post(
            ADJUST,
            {
                "variant": str(v.id),
                "direction": "DECREASE",
                "quantity": 3,
                "reason": "Broken pails discarded after forklift accident",
                "client_adjustment_id": str(uuid.uuid4()),
            },
            format="json",
        )
        assert res.status_code == 201, res.content
        assert InventoryBalance.objects.get(branch=branch, variant=v).quantity == 5

    def test_cross_branch_variant_is_404(self, login_as, owner):
        other = BranchFactory(code="VS58")
        other_owner = OwnerFactory(branch=other)
        v = _variant(other, other_owner, 5)
        res = login_as(owner).post(
            ADJUST,
            {
                "variant": str(v.id),
                "direction": "INCREASE",
                "quantity": 1,
                "reason": "A perfectly detailed reason string here",
                "client_adjustment_id": str(uuid.uuid4()),
            },
            format="json",
        )
        assert res.status_code == 404
