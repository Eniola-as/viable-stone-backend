"""Stage 15 — /push-subscriptions/ API: upsert, multi-device, ownership,
endpoint/key validation, HTTPS rule, public-key exposure, write rate limiting."""

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.tests.factories import EmployeeFactory
from apps.notifications.models import PushSubscription
from apps.notifications.tests.factories import PushSubscriptionFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/push-subscriptions/"


def _body(endpoint="https://push.example.com/sub/abc", **over):
    data = {
        "endpoint": endpoint,
        "p256dh": "B" * 87,
        "auth": "A" * 22,
        "user_agent": "Firefox/128",
    }
    data.update(over)
    return data


class TestRegisterAndUpsert:
    def test_register_returns_201_and_hides_keys(self, login_as, branch, owner):
        res = login_as(owner).post(URL, _body(), format="json")
        assert res.status_code == 201, res.content
        assert "p256dh" not in res.json()
        assert "auth" not in res.json()

    def test_repeat_endpoint_upserts_in_place(self, login_as, branch, owner):
        c = login_as(owner)
        c.post(URL, _body(), format="json")
        again = c.post(URL, _body(p256dh="C" * 87, auth="D" * 22), format="json")
        assert again.status_code == 200
        assert PushSubscription.objects.filter(user=owner).count() == 1
        sub = PushSubscription.objects.get(user=owner)
        assert sub.p256dh == "C" * 87

    def test_multiple_devices_per_user(self, login_as, branch, owner):
        c = login_as(owner)
        c.post(URL, _body(endpoint="https://push.example.com/a"), format="json")
        c.post(URL, _body(endpoint="https://push.example.com/b"), format="json")
        assert PushSubscription.objects.filter(user=owner).count() == 2

    def test_reactivates_a_previously_expired_endpoint(self, login_as, branch, owner):
        sub = PushSubscriptionFactory(user=owner, endpoint="https://push.example.com/x")
        sub.deactivate(expired=True)
        res = login_as(owner).post(
            URL, _body(endpoint="https://push.example.com/x"), format="json"
        )
        assert res.status_code == 200
        sub.refresh_from_db()
        assert sub.is_active is True
        assert sub.expired_at is None


class TestValidation:
    def test_rejects_non_https_remote_endpoint(self, login_as, branch, owner):
        res = login_as(owner).post(
            URL, _body(endpoint="http://push.evil.com/sub"), format="json"
        )
        assert res.status_code == 400

    def test_allows_http_localhost_for_development(self, login_as, branch, owner):
        res = login_as(owner).post(
            URL, _body(endpoint="http://localhost:5173/sub/dev"), format="json"
        )
        assert res.status_code == 201

    def test_rejects_short_keys(self, login_as, branch, owner):
        assert (
            login_as(owner)
            .post(URL, _body(p256dh="tooshort"), format="json")
            .status_code
            == 400
        )
        assert (
            login_as(owner).post(URL, _body(auth="x"), format="json").status_code == 400
        )

    def test_rejects_overlong_endpoint(self, login_as, branch, owner):
        long_endpoint = "https://push.example.com/" + "z" * 600
        res = login_as(owner).post(URL, _body(endpoint=long_endpoint), format="json")
        assert res.status_code == 400


class TestOwnership:
    def test_lists_only_own(self, login_as, branch, owner):
        PushSubscriptionFactory(user=owner)
        PushSubscriptionFactory(user=EmployeeFactory(branch=branch))
        body = login_as(owner).get(URL).json()
        assert body["count"] == 1

    def test_cannot_delete_another_users_subscription(self, login_as, branch, owner):
        victim = PushSubscriptionFactory(user=EmployeeFactory(branch=branch))
        assert login_as(owner).delete(f"{URL}{victim.id}/").status_code == 404

    def test_delete_own_subscription(self, login_as, branch, owner):
        sub = PushSubscriptionFactory(user=owner)
        assert login_as(owner).delete(f"{URL}{sub.id}/").status_code == 204
        assert not PushSubscription.objects.filter(pk=sub.pk).exists()

    def test_deactivate_own_subscription(self, login_as, branch, owner):
        sub = PushSubscriptionFactory(user=owner)
        res = login_as(owner).post(f"{URL}{sub.id}/deactivate/")
        assert res.status_code == 200
        sub.refresh_from_db()
        assert sub.is_active is False

    def test_cannot_deactivate_another_users_subscription(
        self, login_as, branch, owner
    ):
        victim = PushSubscriptionFactory(user=EmployeeFactory(branch=branch))
        assert login_as(owner).post(f"{URL}{victim.id}/deactivate/").status_code == 404


class TestPublicKey:
    @override_settings(
        VAPID_PUBLIC_KEY="BPUBLICKEYbrowsersafe", VAPID_PRIVATE_KEY="s3cr3t"
    )
    def test_public_key_endpoint_exposes_only_the_public_key(
        self, login_as, branch, owner
    ):
        res = login_as(owner).get(f"{URL}public-key/")
        assert res.status_code == 200
        body = res.json()
        assert body == {"public_key": "BPUBLICKEYbrowsersafe"}
        assert "s3cr3t" not in res.content.decode()
        assert "private" not in res.content.decode().lower()


class TestProtectedFieldLeak:
    """Every response shape must be free of p256dh / auth / any VAPID private
    material — asserted explicitly against create, list and retrieve."""

    _SECRETS = ("p256dh", "auth", "vapid_private_key", "private_key")

    def _assert_clean(self, payload):
        as_text = str(payload)
        for secret in self._SECRETS:
            assert secret not in as_text.lower(), secret
        # the exact key values (from _body() and the factory) must never come back
        assert "B" * 87 not in as_text
        assert "A" * 22 not in as_text

    def test_create_response_has_no_secret_fields(self, login_as, branch, owner):
        res = login_as(owner).post(URL, _body(), format="json")
        assert res.status_code == 201
        assert set(res.json()) == {
            "id",
            "endpoint",
            "user_agent",
            "is_active",
            "created_at",
            "last_used_at",
            "expired_at",
        }
        self._assert_clean(res.json())

    def test_list_response_has_no_secret_fields(self, login_as, branch, owner):
        PushSubscriptionFactory(user=owner)
        res = login_as(owner).get(URL)
        assert res.status_code == 200
        self._assert_clean(res.json())

    def test_retrieve_response_has_no_secret_fields(self, login_as, branch, owner):
        sub = PushSubscriptionFactory(user=owner)
        res = login_as(owner).get(f"{URL}{sub.id}/")
        assert res.status_code == 200
        self._assert_clean(res.json())

    def test_deactivate_response_has_no_secret_fields(self, login_as, branch, owner):
        sub = PushSubscriptionFactory(user=owner)
        res = login_as(owner).post(f"{URL}{sub.id}/deactivate/")
        assert res.status_code == 200
        self._assert_clean(res.json())


class TestRateLimit:
    def test_subscription_writes_are_rate_limited(
        self, login_as, branch, owner, monkeypatch
    ):
        from rest_framework.throttling import SimpleRateThrottle

        cache.clear()
        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {**SimpleRateThrottle.THROTTLE_RATES, "notifications_write": "3/min"},
        )
        c = login_as(owner)
        statuses = [
            c.post(
                URL,
                _body(endpoint=f"https://push.example.com/s{i}"),
                format="json",
            ).status_code
            for i in range(5)
        ]
        cache.clear()
        assert statuses.count(201) == 3
        assert statuses[-1] == 429
