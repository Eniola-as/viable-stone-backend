"""Stage 15 — approval request / decision notifications and their recipients."""

import uuid
from decimal import Decimal

import pytest

from apps.accounts.tests.factories import EmployeeFactory, OwnerFactory
from apps.notifications.models import Notification, NotificationType
from apps.sales.services.discounts import (
    approve_discount,
    create_draft_sale,
    reject_discount,
    request_discount,
)
from apps.sales.services.returns import (
    ApprovedLine,
    RefundLine,
    RequestLine,
    approve_return,
    reject_return,
    submit_return_request,
)
from apps.sales.services.sales import CartLine, PaymentLine, create_sale
from apps.sales.tests.factories import stocked_variant

pytestmark = pytest.mark.django_db


def _notes(type_):
    return Notification.objects.filter(notification_type=type_)


def _draft_with_discount_request(branch, owner, cashier):
    variant = stocked_variant(
        branch, owner, price="1000.00", quantity=50, low_stock_level=1
    )
    draft = create_draft_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=5)],
        client_sale_id=uuid.uuid4(),
    )
    approval = request_discount(
        sale=draft, requested_by=cashier, amount=Decimal("1000.00"), reason="loyal"
    )
    return draft, approval


class TestDiscountApprovalNotifications:
    def test_request_notifies_active_owners_only(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        OwnerFactory(branch=branch, is_active=False)
        cashier = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            _draft_with_discount_request(branch, owner, cashier)
        notes = _notes(NotificationType.APPROVAL_REQUESTED)
        assert notes.count() == 1
        assert notes.first().recipient_id == owner.id

    def test_approve_notifies_the_requester(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            _draft, approval = _draft_with_discount_request(branch, owner, cashier)
        with django_capture_on_commit_callbacks(execute=True):
            approve_discount(approval=approval, owner=owner, amount=Decimal("1000.00"))
        notes = _notes(NotificationType.APPROVAL_APPROVED)
        assert notes.count() == 1
        assert notes.first().recipient_id == cashier.id

    def test_reject_notifies_the_requester(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        cashier = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            _draft, approval = _draft_with_discount_request(branch, owner, cashier)
        with django_capture_on_commit_callbacks(execute=True):
            reject_discount(approval=approval, owner=owner, reviewer_note="no")
        notes = _notes(NotificationType.APPROVAL_REJECTED)
        assert notes.count() == 1
        assert notes.first().recipient_id == cashier.id

    def test_no_self_notification_when_owner_is_the_requester(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        # An owner who both raises and approves gets nothing (own action).
        with django_capture_on_commit_callbacks(execute=True):
            _draft, approval = _draft_with_discount_request(branch, owner, owner)
        with django_capture_on_commit_callbacks(execute=True):
            approve_discount(approval=approval, owner=owner, amount=Decimal("500.00"))
        assert not _notes(NotificationType.APPROVAL_APPROVED).exists()
        assert not _notes(NotificationType.APPROVAL_REQUESTED).exists()


class TestReturnApprovalNotifications:
    def _completed_sale(self, branch, owner):
        cashier = EmployeeFactory(branch=branch)
        variant = stocked_variant(
            branch,
            owner,
            price="1000.00",
            quantity=50,
            unit_cost="600.00",
            low_stock_level=1,
        )
        sale = create_sale(
            branch=branch,
            cashier=cashier,
            cart=[CartLine(variant_id=variant.id, quantity=4)],
            payments=[PaymentLine(method="CASH", amount=Decimal("4000.00"))],
            client_sale_id=uuid.uuid4(),
        )
        return sale, sale.items.get()

    def test_submit_return_request_notifies_owners(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        sale, item = self._completed_sale(branch, owner)
        clerk = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            submit_return_request(
                sale=sale,
                requested_by=clerk,
                reason="Customer changed their mind",
                lines=[RequestLine(sale_item_id=item.id, quantity=2)],
                client_return_id=uuid.uuid4(),
            )
        notes = _notes(NotificationType.APPROVAL_REQUESTED)
        assert notes.count() == 1
        assert notes.first().recipient_id == owner.id

    def test_approve_return_notifies_requester(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        sale, item = self._completed_sale(branch, owner)
        clerk = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            approval = submit_return_request(
                sale=sale,
                requested_by=clerk,
                reason="Customer changed their mind",
                lines=[RequestLine(sale_item_id=item.id, quantity=2)],
                client_return_id=uuid.uuid4(),
            )
        with django_capture_on_commit_callbacks(execute=True):
            approve_return(
                approval=approval,
                owner=owner,
                lines=[
                    ApprovedLine(
                        sale_item_id=item.id, quantity=2, condition="RESELLABLE"
                    )
                ],
                refunds=[RefundLine(method="CASH", amount=Decimal("2000.00"))],
            )
        notes = _notes(NotificationType.APPROVAL_APPROVED)
        assert notes.count() == 1
        assert notes.first().recipient_id == clerk.id

    def test_reject_return_notifies_requester(
        self, branch, owner, django_capture_on_commit_callbacks
    ):
        sale, item = self._completed_sale(branch, owner)
        clerk = EmployeeFactory(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            approval = submit_return_request(
                sale=sale,
                requested_by=clerk,
                reason="Customer changed their mind",
                lines=[RequestLine(sale_item_id=item.id, quantity=2)],
                client_return_id=uuid.uuid4(),
            )
        with django_capture_on_commit_callbacks(execute=True):
            reject_return(approval=approval, owner=owner, reviewer_note="not eligible")
        notes = _notes(NotificationType.APPROVAL_REJECTED)
        assert notes.count() == 1
        assert notes.first().recipient_id == clerk.id
