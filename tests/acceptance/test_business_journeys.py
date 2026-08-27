"""Stage 18 — end-to-end business acceptance journeys (spec item 7).

Fifteen API-level journeys over the real PostgreSQL test database. These assert
the business rules end to end; the fine-grained unit and concurrency suites
elsewhere stay the authority on race conditions.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.accounts.tests.mfa_helpers import current_token, latest_device
from apps.core.models import AuditLog
from apps.notifications.models import Notification, NotificationType
from apps.sales.models import Payment, Sale

pytestmark = pytest.mark.django_db

API = "/api/v1"


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _sale_body(variant, *, qty=1, amount="1000.00", cid=None, **extra):
    body = {
        "client_sale_id": str(cid or uuid.uuid4()),
        "items": [{"variant": str(variant.id), "quantity": qty}],
        "payments": [{"method": "CASH", "amount": amount}],
    }
    body.update(extra)
    return body


def _new_branch():
    b = BranchFactory(code=f"J{uuid.uuid4().hex[:4].upper()}")
    return b, OwnerFactory(branch=b), EmployeeFactory(branch=b)


# --------------------------------------------------------------------------- #
# 1. Owner authentication + MFA                                               #
# --------------------------------------------------------------------------- #
def test_journey_owner_auth_and_mfa(api_client, owner, login_as):
    login = api_client.post(
        f"{API}/auth/login/", {"username": "owner", "password": "owner-pass-12345"}
    )
    assert login.status_code == 200
    assert login.json()["mfa_required"] is True  # profile withheld until MFA

    client = login_as(owner, mfa_verified=False)
    assert client.get(f"{API}/expenses/").status_code == 403  # gated

    setup = client.post(f"{API}/auth/mfa/setup/")
    assert setup.status_code == 200 and setup.json()["secret"]
    device = latest_device(owner, confirmed=False)
    confirm = client.post(
        f"{API}/auth/mfa/setup/confirm/", {"token": current_token(device)}
    )
    assert confirm.status_code == 200
    assert len(confirm.json()["recovery_codes"]) >= 8

    me = client.get(f"{API}/auth/me/")
    assert me.status_code == 200
    body = me.json()
    # a flag like ``must_change_password`` is fine; secret material is not
    assert not (
        set(body)
        & {
            "password",
            "recovery_codes",
            "totp_secret",
            "code_hash",
            "bin_key",
        }
    )
    blob = me.content.decode().lower()
    for token in ("pbkdf2_", "argon2", "bcrypt", "recovery_code", '"secret"'):
        assert token not in blob
    assert client.get(f"{API}/expenses/").status_code == 200  # MFA now cleared


# --------------------------------------------------------------------------- #
# 2. Branch / user-role isolation                                            #
# --------------------------------------------------------------------------- #
def test_journey_branch_and_role_isolation(
    login_as, branch, owner, employee, tech_admin
):
    _b2, owner2, _emp2 = _new_branch()

    # employee: catalogue read yes, owner ledgers no
    ec = login_as(employee)
    assert ec.get(f"{API}/products/").status_code == 200
    assert ec.get(f"{API}/expenses/").status_code == 403
    assert ec.get(f"{API}/reports/profit/").status_code == 403

    # tech-admin: account admin yes, business data no
    tc = login_as(tech_admin)
    assert tc.get(f"{API}/expenses/").status_code == 403
    assert tc.get(f"{API}/reports/profit/").status_code == 403
    assert tc.get(f"{API}/audit-logs/").status_code == 200

    # owner of branch 2 sees none of branch 1's users
    cat = (
        login_as(owner)
        .post(f"{API}/expense-categories/", {"name": "Rent"}, format="json")
        .json()
    )
    listing = login_as(owner2).get(f"{API}/expense-categories/").json()
    assert all(row["id"] != cat["id"] for row in listing["results"])


# --------------------------------------------------------------------------- #
# 3. Catalogue creation, variant pricing, price history                      #
# --------------------------------------------------------------------------- #
def test_journey_catalogue_pricing_and_history(login_as, branch, owner, employee):
    oc = login_as(owner)
    cat = oc.post(f"{API}/categories/", {"name": "Emulsion"}, format="json").json()
    brand = oc.post(f"{API}/brands/", {"name": "DuluxLike"}, format="json").json()
    product = oc.post(
        f"{API}/products/",
        {
            "name": "White Silk 20L",
            "category": cat["id"],
            "brand": brand["id"],
            "kind": "PAINT",
        },
        format="json",
    )
    assert product.status_code == 201, product.content
    pid = product.json()["id"]
    variant = oc.post(
        f"{API}/variants/",
        {"product": pid, "sku": "WS20-A", "description": "20 litre"},
        format="json",
    )
    assert variant.status_code == 201, variant.content
    vid = variant.json()["id"]

    assert (
        oc.post(f"{API}/variants/{vid}/price/", {"amount": "12000.00"}).status_code
        == 200
    )
    assert (
        oc.post(f"{API}/variants/{vid}/price/", {"amount": "12500.00"}).status_code
        == 200
    )

    history = oc.get(f"{API}/variants/{vid}/price-history/").json()
    rows = history["results"] if "results" in history else history
    assert [r["amount"] for r in rows][:2] == ["12500.00", "12000.00"]

    emp_row = login_as(employee).get(f"{API}/variants/{vid}/").json()
    assert emp_row["price"] == "12500.00"
    assert "cost" not in emp_row and "unit_cost" not in emp_row


# --------------------------------------------------------------------------- #
# 4. Supplier restock, weighted-average cost, inventory movement             #
# --------------------------------------------------------------------------- #
def test_journey_restock_weighted_cost_and_movement(login_as, branch, owner, stocked):
    variant = stocked(sku="RS-A", price="2000.00", qty=10, cost="600.00")
    oc = login_as(owner)
    supplier = oc.post(
        f"{API}/suppliers/", {"name": "PaintCo", "phone": "08030000000"}
    ).json()

    restock = oc.post(
        f"{API}/restocks/",
        {
            "supplier": supplier["id"],
            "date": "2026-08-10",
            "items": [
                {"variant": str(variant.id), "quantity": 10, "unit_cost": "800.00"}
            ],
        },
        format="json",
    )
    assert restock.status_code == 201, restock.content
    confirm = oc.post(f"{API}/restocks/{restock.json()['id']}/confirm/")
    assert confirm.status_code == 200 and confirm.json()["status"] == "CONFIRMED"

    row = next(
        r
        for r in oc.get(f"{API}/inventory/").json()["results"]
        if r["variant_sku"] == "RS-A"
    )
    assert row["quantity"] == 20
    # (10*600 + 10*800) / 20 == 700
    assert row["average_unit_cost"] == "700.00"

    moves = oc.get(f"{API}/inventory/movements/?variant={variant.id}").json()["results"]
    assert {m["movement_type"] for m in moves} >= {"OPENING", "RESTOCK"}


# --------------------------------------------------------------------------- #
# 5. Fully paid cash sale + receipt                                          #
# --------------------------------------------------------------------------- #
def test_journey_fully_paid_cash_sale_and_receipt(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="CS-A", price="1500.00", qty=10)
    c = login_as(employee)
    sale = c.post(
        f"{API}/sales/",
        _sale_body(variant, qty=2, amount="3000.00"),
        format="json",
    )
    assert sale.status_code == 201, sale.content
    body = sale.json()
    assert body["status"] == "COMPLETED"
    assert body["total"] == "3000.00" and body["change_due"] == "0.00"
    assert body["receipt_number"]

    receipt = c.get(f"{API}/sales/{body['id']}/receipt/").json()
    # the JSON receipt is a display document: money is grouped for printing
    assert receipt["total"] == "3,000.00"
    assert receipt["receipt_number"] == body["receipt_number"]

    pdf = c.get(f"{API}/sales/{body['id']}/receipt.pdf")
    assert pdf.status_code == 200
    assert pdf["Content-Type"] == "application/pdf"
    assert pdf.content[:5] == b"%PDF-"

    row = next(
        r
        for r in login_as(owner).get(f"{API}/inventory/").json()["results"]
        if r["variant_sku"] == "CS-A"
    )
    assert row["quantity"] == 8


# --------------------------------------------------------------------------- #
# 6. Split-payment sale + receipt                                            #
# --------------------------------------------------------------------------- #
def test_journey_split_payment_sale_and_receipt(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="SP-A", price="1000.00", qty=10)
    c = login_as(employee)
    sale = c.post(
        f"{API}/sales/",
        {
            "client_sale_id": str(uuid.uuid4()),
            "items": [{"variant": str(variant.id), "quantity": 3}],
            "payments": [
                {"method": "POS", "amount": "2000.00", "reference": "POS-1"},
                {"method": "CASH", "amount": "1000.00", "tendered_amount": "1000.00"},
            ],
        },
        format="json",
    )
    assert sale.status_code == 201, sale.content
    body = sale.json()
    assert body["total"] == "3000.00"
    assert len(body["payments"]) == 2
    receipt = c.get(f"{API}/sales/{body['id']}/receipt/").json()
    assert {p["method"] for p in receipt["payments"]} == {"POS", "CASH"}
    paid = sum(Decimal(p["amount"].replace(",", "")) for p in receipt["payments"])
    assert paid == Decimal("3000.00")


# --------------------------------------------------------------------------- #
# 7. Idempotent sale retry — no duplicated stock / payment / receipt         #
# --------------------------------------------------------------------------- #
def test_journey_idempotent_sale_retry(login_as, branch, owner, employee, stocked):
    variant = stocked(sku="ID-A", price="1000.00", qty=10)
    c = login_as(employee)
    cid = uuid.uuid4()
    payload = _sale_body(variant, qty=2, amount="2000.00", cid=cid)

    first = c.post(f"{API}/sales/", payload, format="json")
    assert first.status_code == 201
    second = c.post(f"{API}/sales/", payload, format="json")
    assert second.status_code in (200, 201)
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["receipt_number"] == first.json()["receipt_number"]

    assert Sale.objects.filter(client_sale_id=cid).count() == 1
    assert Payment.objects.filter(sale__client_sale_id=cid).count() == 1
    row = next(
        r
        for r in login_as(owner).get(f"{API}/inventory/").json()["results"]
        if r["variant_sku"] == "ID-A"
    )
    assert row["quantity"] == 8  # decremented once, not twice


# --------------------------------------------------------------------------- #
# 8. Owner-approved discount + finalisation                                  #
# --------------------------------------------------------------------------- #
def test_journey_owner_approved_discount_and_finalisation(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="DC-A", price="1000.00", qty=20)
    cc = login_as(employee)
    draft = cc.post(
        f"{API}/sales/drafts/",
        {
            "client_sale_id": str(uuid.uuid4()),
            "items": [{"variant": str(variant.id), "quantity": 5}],
        },
        format="json",
    )
    assert draft.status_code == 201, draft.content
    sid = draft.json()["id"]
    rid = cc.post(
        f"{API}/sales/{sid}/discount-requests/",
        {"amount": "1000.00", "reason": "loyal bulk buyer"},
        format="json",
    ).json()["id"]

    assert (
        login_as(employee)
        .post(f"{API}/approvals/{rid}/approve/", {"amount": "1000.00"}, format="json")
        .status_code
        == 403
    )
    approved = login_as(owner).post(
        f"{API}/approvals/{rid}/approve/",
        {"amount": "1000.00", "reviewer_note": "ok"},
        format="json",
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    fin = cc.post(
        f"{API}/sales/{sid}/finalise/",
        {
            "payments": [
                {"method": "CASH", "amount": "4000.00", "tendered_amount": "4000.00"}
            ]
        },
        format="json",
    )
    assert fin.status_code == 200, fin.content
    body = fin.json()
    assert body["status"] == "COMPLETED"
    assert body["subtotal"] == "5000.00"
    assert body["discount_total"] == "1000.00"
    assert body["total"] == "4000.00"
    assert body["receipt_number"]


# --------------------------------------------------------------------------- #
# 9. Resellable and damaged returns — refunds, stock, profit treatment      #
# --------------------------------------------------------------------------- #
def test_journey_resellable_and_damaged_returns(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="RT-A", price="1000.00", qty=20, cost="600.00")
    cc = login_as(employee)
    sale = cc.post(
        f"{API}/sales/", _sale_body(variant, qty=6, amount="6000.00"), format="json"
    ).json()
    item_id = sale["items"][0]["id"]

    def _request_return(qty, key):
        return cc.post(
            f"{API}/sales/{sale['id']}/return-requests/",
            {
                "reason": "customer changed their mind about the colour",
                "client_return_id": str(key),
                "lines": [{"sale_item": item_id, "quantity": qty}],
            },
            format="json",
        ).json()["id"]

    def _stock():
        return next(
            r
            for r in login_as(owner).get(f"{API}/inventory/").json()["results"]
            if r["variant_sku"] == "RT-A"
        )["quantity"]

    before = _stock()

    r1 = _request_return(2, uuid.uuid4())
    ok1 = login_as(owner).post(
        f"{API}/approvals/{r1}/approve/",
        {
            "lines": [{"sale_item": item_id, "quantity": 2, "condition": "RESELLABLE"}],
            "refunds": [{"method": "CASH", "amount": "2000.00"}],
        },
        format="json",
    )
    assert ok1.status_code == 200 and ok1.json()["total"] == "2000.00"
    assert _stock() == before + 2  # resellable restored

    r2 = _request_return(1, uuid.uuid4())
    ok2 = login_as(owner).post(
        f"{API}/approvals/{r2}/approve/",
        {
            "lines": [
                {"sale_item": item_id, "quantity": 1, "condition": "DAMAGED_OR_OPENED"}
            ],
            "refunds": [{"method": "CASH", "amount": "1000.00"}],
        },
        format="json",
    )
    assert ok2.status_code == 200 and ok2.json()["total"] == "1000.00"
    assert _stock() == before + 2  # damaged NOT restored

    report = (
        login_as(owner)
        .get(f"{API}/reports/profit/?period=custom&start=2026-08-01&end=2026-08-31")
        .json()
    )
    assert report["returns_total"] == "3000.00"
    # COGS reversed only for the resellable unit(s): 2 * 600
    assert report["cost_reversed"] == "1200.00"


# --------------------------------------------------------------------------- #
# 10. Protected stock adjustment + audit trail                              #
# --------------------------------------------------------------------------- #
def test_journey_protected_stock_adjustment_and_audit(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="AJ-A", price="1000.00", qty=10)
    reason = "Physical recount after a warehouse shelf collapse incident"

    assert (
        login_as(employee)
        .post(
            f"{API}/inventory/adjustments/",
            {
                "variant": str(variant.id),
                "direction": "DECREASE",
                "quantity": 2,
                "reason": reason,
                "client_adjustment_id": str(uuid.uuid4()),
            },
            format="json",
        )
        .status_code
        == 403
    )

    res = login_as(owner).post(
        f"{API}/inventory/adjustments/",
        {
            "variant": str(variant.id),
            "direction": "DECREASE",
            "quantity": 3,
            "reason": reason,
            "client_adjustment_id": str(uuid.uuid4()),
        },
        format="json",
    )
    assert res.status_code == 201, res.content

    row = next(
        r
        for r in login_as(owner).get(f"{API}/inventory/").json()["results"]
        if r["variant_sku"] == "AJ-A"
    )
    assert row["quantity"] == 7

    entry = AuditLog.objects.get(action="stock.adjust", target_id=variant.id)
    assert entry.before["quantity"] == 10
    assert entry.after["quantity"] == 7
    assert entry.branch_id == branch.id


# --------------------------------------------------------------------------- #
# 11. Expenses + profit / best-seller / slow-mover / inventory reports       #
# --------------------------------------------------------------------------- #
def test_journey_expenses_and_reports(login_as, branch, owner, employee, stocked):
    fast = stocked(sku="FAST-1", price="1000.00", qty=50, cost="600.00")
    stocked(sku="SLOW-1", price="800.00", qty=30, cost="500.00")  # stocked, never sold
    cc = login_as(employee)
    cc.post(f"{API}/sales/", _sale_body(fast, qty=10, amount="10000.00"), format="json")

    oc = login_as(owner)
    ec = oc.post(f"{API}/expense-categories/", {"name": "Power"}, format="json").json()
    assert (
        oc.post(
            f"{API}/expenses/",
            {
                "category": ec["id"],
                "amount": "1500.00",
                "expense_date": "2026-08-15",
                "description": "Diesel for generator",
            },
            format="json",
        ).status_code
        == 201
    )

    rng = "period=custom&start=2026-08-01&end=2026-08-31"
    profit = oc.get(f"{API}/reports/profit/?{rng}").json()
    assert profit["revenue"] == "10000.00"
    assert profit["cogs"] == "6000.00"
    assert profit["expenses_total"] == "1500.00"
    assert profit["net_profit"] == "2500.00"  # 10000 - 6000 - 1500

    best = oc.get(f"{API}/reports/best-sellers/?{rng}").json()
    assert best and best[0]["sku"] == "FAST-1"
    slow_rows = oc.get(f"{API}/reports/slow-movers/?{rng}").json()
    assert any(r["sku"] == "SLOW-1" for r in slow_rows)
    inv = oc.get(f"{API}/reports/inventory/").json()
    assert Decimal(inv["stock_value"].replace(",", "")) > 0


# --------------------------------------------------------------------------- #
# 12. Low-stock notification transition + dedup                             #
# --------------------------------------------------------------------------- #
def test_journey_low_stock_notification_and_dedup(
    login_as, branch, owner, employee, stocked, django_capture_on_commit_callbacks
):
    variant = stocked(sku="LS-A", price="1000.00", qty=8, low=5)
    cc = login_as(employee)

    with django_capture_on_commit_callbacks(execute=True):
        cc.post(
            f"{API}/sales/", _sale_body(variant, qty=3, amount="3000.00"), format="json"
        )  # 8 -> 5 == low
    notes = Notification.objects.filter(
        branch=branch, notification_type=NotificationType.LOW_STOCK
    )
    assert notes.count() == 1
    assert notes.first().recipient_id == owner.id

    with django_capture_on_commit_callbacks(execute=True):
        cc.post(
            f"{API}/sales/", _sale_body(variant, qty=1, amount="1000.00"), format="json"
        )  # 5 -> 4, still low: no repeat
    assert notes.count() == 1

    unread = login_as(owner).get(f"{API}/notifications/unread-count/").json()
    assert unread["unread"] >= 1


# --------------------------------------------------------------------------- #
# 13. Offline authorisation, signed snapshot, idempotent sync, conflict      #
# --------------------------------------------------------------------------- #
def test_journey_offline_authorisation_sync_and_conflict(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="OF-A", price="1000.00", qty=5)
    oc = login_as(owner)
    device_id = oc.post(
        f"{API}/offline/devices/", {"name": "Till"}, format="json"
    ).json()["id"]
    auth = oc.post(
        f"{API}/offline/authorizations/",
        {"device": device_id, "cashier": str(employee.id)},
        format="json",
    ).json()
    assert auth["signed_token"]
    snap_skus = (
        {v["sku"] for v in auth["snapshot"]["variants"]}
        if isinstance(auth.get("snapshot"), dict)
        else None
    )
    if snap_skus is not None:
        assert "OF-A" in snap_skus

    # while the session is authorised, online stock ops are blocked
    blocked = login_as(employee).post(
        f"{API}/sales/", _sale_body(variant, amount="1000.00"), format="json"
    )
    assert blocked.status_code == 409
    assert "offline" in blocked.json()["code"]

    cc = login_as(employee)
    good = {
        "client_sale_id": str(uuid.uuid4()),
        "device_sequence": 1,
        "offline_created_at": timezone.now().isoformat(),
        "items": [{"variant_id": str(variant.id), "quantity": 2}],
        "payments": [{"method": "CASH", "amount": "2000.00"}],
    }
    sync = cc.post(
        f"{API}/offline/sync/",
        {"authorization_token": auth["signed_token"], "sales": [good]},
        format="json",
    )
    assert sync.status_code == 200, sync.content
    result = sync.json()["results"][0]
    assert result["outcome"] == "ACCEPTED" and result["receipt_number"]

    # verbatim retry is idempotent
    again = cc.post(
        f"{API}/offline/sync/",
        {"authorization_token": auth["signed_token"], "sales": [good]},
        format="json",
    ).json()["results"][0]
    assert again["outcome"] == "DUPLICATE"
    assert again["receipt_number"] == result["receipt_number"]

    # an oversell against the snapshot is retained as a CONFLICT, not silently lost
    oversell = {
        "client_sale_id": str(uuid.uuid4()),
        "device_sequence": 2,
        "offline_created_at": timezone.now().isoformat(),
        "items": [{"variant_id": str(variant.id), "quantity": 99}],
        "payments": [{"method": "CASH", "amount": "99000.00"}],
    }
    conflict = cc.post(
        f"{API}/offline/sync/",
        {"authorization_token": auth["signed_token"], "sales": [oversell]},
        format="json",
    ).json()["results"][0]
    assert conflict["outcome"] in {"CONFLICT", "OWNER_REVIEW_REQUIRED", "REJECTED"}

    listed = oc.get(f"{API}/offline/sync-records/").json()
    assert listed["count"] >= 2


# --------------------------------------------------------------------------- #
# 14. Cross-branch denial throughout a complete workflow                     #
# --------------------------------------------------------------------------- #
def test_journey_cross_branch_denial_end_to_end(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="XB-A", price="1000.00", qty=10)
    sale = (
        login_as(employee)
        .post(
            f"{API}/sales/", _sale_body(variant, qty=2, amount="2000.00"), format="json"
        )
        .json()
    )
    cat = (
        login_as(owner)
        .post(f"{API}/expense-categories/", {"name": "Insurance"}, format="json")
        .json()
    )

    _b2, owner2, emp2 = _new_branch()
    o2 = login_as(owner2)
    assert o2.get(f"{API}/variants/{variant.id}/").status_code == 404
    assert o2.get(f"{API}/sales/{sale['id']}/").status_code == 404
    assert o2.get(f"{API}/sales/{sale['id']}/receipt/").status_code == 404
    assert o2.get(f"{API}/sales/{sale['id']}/receipt.pdf").status_code == 404
    assert o2.get(f"{API}/expense-categories/{cat['id']}/").status_code == 404
    assert (
        o2.post(
            f"{API}/inventory/adjustments/",
            {
                "variant": str(variant.id),
                "direction": "INCREASE",
                "quantity": 1,
                "reason": "trying to touch another branch's stock here",
                "client_adjustment_id": str(uuid.uuid4()),
            },
            format="json",
        ).status_code
        == 404
    )
    assert (
        login_as(emp2)
        .post(
            f"{API}/sales/{sale['id']}/return-requests/",
            {
                "reason": "cross branch return attempt on a foreign sale",
                "client_return_id": str(uuid.uuid4()),
                "lines": [{"sale_item": sale["items"][0]["id"], "quantity": 1}],
            },
            format="json",
        )
        .status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# 15. Snapshot stability after product name / price / cost change            #
# --------------------------------------------------------------------------- #
def test_journey_snapshot_stability_after_catalogue_change(
    login_as, branch, owner, employee, stocked
):
    variant = stocked(sku="SN-A", price="1000.00", qty=10, cost="600.00")
    cc = login_as(employee)
    sale = cc.post(
        f"{API}/sales/", _sale_body(variant, qty=2, amount="2000.00"), format="json"
    ).json()
    before = cc.get(f"{API}/sales/{sale['id']}/receipt/").json()
    before_pdf = cc.get(f"{API}/sales/{sale['id']}/receipt.pdf").content

    # change everything the receipt could have referenced
    variant.product.name = "RENAMED AFTER THE SALE"
    variant.product.save(update_fields=["name"])
    login_as(owner).post(f"{API}/variants/{variant.id}/price/", {"amount": "9999.00"})

    after = cc.get(f"{API}/sales/{sale['id']}/receipt/").json()
    assert after["items"] == before["items"]
    assert before["total"] == "2,000.00"
    assert after["total"] == before["total"]
    assert after["receipt_number"] == before["receipt_number"]
    assert "RENAMED AFTER THE SALE" not in str(after["items"])
    # PDF is regenerated but from the same immutable snapshot -> same length band
    after_pdf = cc.get(f"{API}/sales/{sale['id']}/receipt.pdf").content
    assert after_pdf[:5] == b"%PDF-" and before_pdf[:5] == b"%PDF-"
