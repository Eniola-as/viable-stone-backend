"""Stage 15 — /notifications/ API: ownership, unread count, idempotent reads."""

import pytest

from apps.accounts.tests.factories import BranchFactory, EmployeeFactory, OwnerFactory
from apps.notifications.models import Notification, NotificationType
from apps.notifications.tests.factories import NotificationFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/notifications/"


class TestListAndOwnership:
    def test_lists_only_own_notifications(self, login_as, branch, owner):
        mine = NotificationFactory(recipient=owner, branch=branch)
        NotificationFactory(recipient=EmployeeFactory(branch=branch), branch=branch)
        body = login_as(owner).get(URL).json()
        ids = [row["id"] for row in body["results"]]
        assert ids == [str(mine.id)]

    def test_other_users_notification_is_404(self, login_as, branch, owner):
        someone_else = NotificationFactory(
            recipient=EmployeeFactory(branch=branch), branch=branch
        )
        res = login_as(owner).get(f"{URL}{someone_else.id}/")
        assert res.status_code == 404

    def test_cross_branch_notification_is_404(self, login_as, owner):
        other_branch = BranchFactory(code="VS77")
        other_owner = OwnerFactory(branch=other_branch)
        note = NotificationFactory(recipient=other_owner, branch=other_branch)
        assert login_as(owner).get(f"{URL}{note.id}/").status_code == 404

    def test_serializer_exposes_no_sensitive_fields(self, login_as, branch, owner):
        NotificationFactory(recipient=owner, branch=branch)
        row = login_as(owner).get(URL).json()["results"][0]
        assert set(row) == {
            "id",
            "notification_type",
            "title",
            "message",
            "is_read",
            "read_at",
            "created_at",
            "action_path",
            "related_object_type",
            "related_object_id",
        }


class TestUnreadCount:
    def test_unread_count(self, login_as, branch, owner):
        NotificationFactory.create_batch(3, recipient=owner, branch=branch)
        NotificationFactory(recipient=owner, branch=branch, is_read=True)
        res = login_as(owner).get(f"{URL}unread-count/")
        assert res.status_code == 200
        assert res.json() == {"unread": 3}


class TestMarkRead:
    def test_mark_read_is_idempotent(self, login_as, branch, owner):
        note = NotificationFactory(recipient=owner, branch=branch)
        c = login_as(owner)
        first = c.post(f"{URL}{note.id}/read/")
        assert first.status_code == 200
        assert first.json()["is_read"] is True
        read_at = first.json()["read_at"]
        second = c.post(f"{URL}{note.id}/read/")
        assert second.status_code == 200
        assert second.json()["read_at"] == read_at

    def test_cannot_mark_another_users_notification(self, login_as, branch, owner):
        note = NotificationFactory(
            recipient=EmployeeFactory(branch=branch), branch=branch
        )
        assert login_as(owner).post(f"{URL}{note.id}/read/").status_code == 404

    def test_read_all_is_idempotent(self, login_as, branch, owner):
        NotificationFactory.create_batch(4, recipient=owner, branch=branch)
        c = login_as(owner)
        first = c.post(f"{URL}read-all/")
        assert first.status_code == 200
        assert first.json() == {"updated": 4}
        assert c.post(f"{URL}read-all/").json() == {"updated": 0}
        assert not Notification.objects.filter(recipient=owner, is_read=False).exists()

    def test_read_all_only_touches_own_rows(self, login_as, branch, owner):
        other = EmployeeFactory(branch=branch)
        NotificationFactory(recipient=other, branch=branch)
        NotificationFactory(recipient=owner, branch=branch)
        login_as(owner).post(f"{URL}read-all/")
        assert Notification.objects.filter(recipient=other, is_read=False).count() == 1


class TestReadOnly:
    def test_clients_cannot_create(self, login_as, branch, owner):
        res = login_as(owner).post(
            URL,
            {
                "notification_type": NotificationType.LOW_STOCK,
                "title": "x",
                "message": "y",
                "dedupe_key": "z",
            },
            format="json",
        )
        assert res.status_code == 405

    def test_clients_cannot_delete_or_edit(self, login_as, branch, owner):
        note = NotificationFactory(recipient=owner, branch=branch)
        c = login_as(owner)
        assert c.delete(f"{URL}{note.id}/").status_code == 405
        assert c.patch(f"{URL}{note.id}/", {"title": "hacked"}).status_code == 405

    def test_requires_authentication(self, api_client):
        assert api_client.get(URL).status_code == 401
