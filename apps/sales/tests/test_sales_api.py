import uuid

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.sales.models import Customer, Sale
from apps.sales.services.sales import CartLine, PaymentLine, create_sale

from .factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales/"
CUSTOMERS = "/api/v1/customers/"


def _payload(variant, qty, amount, **extra):
    body = {
        "client_sale_id": str(uuid.uuid4()),
        "items": [{"variant": str(variant.id), "quantity": qty}],
        "payments": [{"method": "CASH", "amount": amount, "tendered_amount": amount}],
    }
    body.update(extra)
    return body


class TestCreateSaleApi:
    def test_employee_creates_cash_sale(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1200.00", quantity=10)
        res = login_as(emp).post(SALES, _payload(variant, 2, "2400.00"), format="json")
        assert res.status_code == 201, res.content
        body = res.json()
        assert body["status"] == "COMPLETED"
        assert body["total"] == "2400.00"
        assert body["receipt_number"]
        assert body["items"][0]["unit_price_snapshot"] == "1200.00"

    def test_employee_sale_response_hides_cost(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        body = (
            login_as(emp)
            .post(SALES, _payload(variant, 1, "1000.00"), format="json")
            .json()
        )
        assert "unit_cost_snapshot" not in body["items"][0]
        assert str(body).find("cost") == -1

    def test_owner_sale_response_includes_cost_snapshot(self, login_as, branch, owner):
        variant = stocked_variant(
            branch, owner, price="1000.00", quantity=5, unit_cost="640.00"
        )
        body = (
            login_as(owner)
            .post(SALES, _payload(variant, 1, "1000.00"), format="json")
            .json()
        )
        assert body["items"][0]["unit_cost_snapshot"] == "640.00"

    def test_split_payment_via_api(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        payload = {
            "client_sale_id": str(uuid.uuid4()),
            "items": [{"variant": str(variant.id), "quantity": 3}],
            "payments": [
                {"method": "POS", "amount": "1500.00", "reference": "POS-9"},
                {"method": "TRANSFER", "amount": "1500.00", "reference": "TRX-9"},
            ],
        }
        res = login_as(emp).post(SALES, payload, format="json")
        assert res.status_code == 201
        assert len(res.json()["payments"]) == 2

    def test_payment_mismatch_returns_400_envelope(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        res = login_as(emp).post(SALES, _payload(variant, 2, "1999.99"), format="json")
        assert res.status_code == 400
        assert res.json()["code"] == "payment_mismatch"

    def test_insufficient_stock_returns_409(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=1)
        res = login_as(emp).post(SALES, _payload(variant, 5, "5000.00"), format="json")
        assert res.status_code == 409
        assert res.json()["code"] == "stock_not_available"
        assert not Sale.objects.exists()

    def test_idempotent_retry_same_key_returns_same_sale(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=10)
        body = _payload(variant, 2, "2000.00")
        first = login_as(emp).post(SALES, body, format="json")
        second = login_as(emp).post(SALES, body, format="json")
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert Sale.objects.count() == 1

    def test_sale_with_walk_in_customer_creates_no_customer_row(
        self, login_as, branch, owner
    ):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        payload = _payload(variant, 1, "1000.00", customer={"name": "", "phone": ""})
        res = login_as(emp).post(SALES, payload, format="json")
        assert res.status_code == 201
        assert res.json()["customer"] is None
        assert not Customer.objects.exists()

    def test_sale_with_named_customer(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        variant = stocked_variant(branch, owner, price="1000.00", quantity=5)
        payload = _payload(
            variant, 1, "1000.00", customer={"name": "Ada", "phone": "0803 111 2222"}
        )
        res = login_as(emp).post(SALES, payload, format="json")
        assert res.status_code == 201
        customer = Customer.objects.get()
        assert customer.name == "Ada"
        assert customer.phone == "08031112222"


class TestSaleVisibility:
    def _make_sale(self, branch, cashier, owner):
        variant = stocked_variant(branch, owner, price="500.00", quantity=20)
        return create_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=variant.id, quantity=1)],
            payments=[
                PaymentLine(method="CASH", amount="500.00", tendered_amount="500.00")
            ],
            client_sale_id=uuid.uuid4(),
        )

    def test_cashier_sees_only_their_own_sales(self, login_as, branch, owner):
        me = EmployeeFactory(branch=branch, username="me")
        other = EmployeeFactory(branch=branch, username="other")
        mine = self._make_sale(branch, me, owner)
        theirs = self._make_sale(branch, other, owner)

        body = login_as(me).get(SALES).json()
        ids = {row["id"] for row in body["results"]}
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids

    def test_owner_sees_all_branch_sales(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        self._make_sale(branch, emp, owner)
        self._make_sale(branch, owner, owner)
        assert login_as(owner).get(SALES).json()["count"] == 2

    def test_cross_branch_sale_id_is_404(self, login_as, branch, owner):
        other_branch = BranchFactory(code="VS61")
        other_owner = OwnerFactory(branch=other_branch)
        other_emp = EmployeeFactory(branch=other_branch)
        outside = self._make_sale(other_branch, other_emp, other_owner)
        assert login_as(owner).get(f"{SALES}{outside.id}/").status_code == 404

    def test_sales_are_not_deletable(self, login_as, branch, owner):
        emp = EmployeeFactory(branch=branch)
        sale = self._make_sale(branch, emp, owner)
        assert login_as(owner).delete(f"{SALES}{sale.id}/").status_code == 405


class TestCustomerApi:
    def test_customer_list_masks_phone_for_employee(self, login_as, branch):
        emp = EmployeeFactory(branch=branch)
        Customer.objects.create(branch=branch, name="Ada", phone="08031112222")
        row = login_as(emp).get(CUSTOMERS).json()["results"][0]
        assert row["phone"] == "*******2222"  # 11 digits -> 7 masked + last 4

    def test_owner_customer_list_shows_full_phone(self, login_as, branch, owner):
        Customer.objects.create(branch=branch, name="Ada", phone="08031112222")
        row = login_as(owner).get(CUSTOMERS).json()["results"][0]
        assert row["phone"] == "08031112222"

    def test_blank_customer_rejected(self, login_as, branch):
        emp = EmployeeFactory(branch=branch)
        res = login_as(emp).post(CUSTOMERS, {"name": "", "phone": ""})
        assert res.status_code == 400
