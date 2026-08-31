"""G9 — DraftCreateRequest.customer is typed {name?, phone?}, not free-form.

The draft path already accepts the same ``{name, phone}`` object as
``POST /sales/``; this only replaces the free-form ``object`` schema with the
shared ``CustomerInlineRequest`` type. Compatible runtime behaviour is
unchanged (omitted / null customer -> walk-in).
"""

import uuid

import pytest

from apps.accounts.tests.factories import EmployeeFactory
from apps.sales.models import Customer
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales"


def _draft_body(variant, customer=...):
    body = {
        "client_sale_id": str(uuid.uuid4()),
        "items": [{"variant": str(variant.id), "quantity": 1}],
    }
    if customer is not ...:
        body["customer"] = customer
    return body


class TestRuntime:
    def test_name_phone_customer_is_attached(self, login_as, branch, owner):
        cc = login_as(EmployeeFactory(branch=branch))
        v = stocked_variant(branch, owner, price="1000.00", quantity=5)
        res = cc.post(
            f"{SALES}/drafts/",
            _draft_body(v, {"name": "Ada", "phone": "0803 111 2222"}),
            format="json",
        )
        assert res.status_code == 201, res.content
        assert res.json()["customer_name"] == "Ada"
        assert Customer.objects.filter(branch=branch, name="Ada").exists()

    def test_omitted_and_null_customer_are_walk_in(self, login_as, branch, owner):
        cc = login_as(EmployeeFactory(branch=branch))
        v = stocked_variant(branch, owner, price="1000.00", quantity=5)
        assert (
            cc.post(f"{SALES}/drafts/", _draft_body(v), format="json").json()[
                "customer"
            ]
            is None
        )
        assert (
            cc.post(f"{SALES}/drafts/", _draft_body(v, None), format="json").json()[
                "customer"
            ]
            is None
        )


class TestOpenAPI:
    def test_draft_create_request_customer_is_customer_inline(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        draft_req = schema["components"]["schemas"]["DraftCreateRequest"]["properties"]
        assert "$ref" in str(draft_req["customer"])
        assert "CustomerInline" in str(draft_req["customer"])
