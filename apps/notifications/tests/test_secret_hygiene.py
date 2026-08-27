"""Stage 15 corrections — no subscription auth keys or VAPID private material
leak through the API schema, openapi.yml, .env.example or settings fallbacks."""

import re
from pathlib import Path

import pytest
import yaml
from django.conf import settings
from drf_spectacular.generators import SchemaGenerator

ROOT = Path(settings.BASE_DIR)

_SECRET_PROPERTY_NAMES = {"p256dh", "auth", "vapid_private_key", "private_key"}


def _iter_schema_properties(schema: dict):
    for name, component in schema.get("components", {}).get("schemas", {}).items():
        props = component.get("properties", {})
        yield name, set(props)


class TestSchemaHasNoSecrets:
    def test_response_schemas_never_expose_subscription_keys(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        offenders = []
        for name, props in _iter_schema_properties(schema):
            # request bodies legitimately carry p256dh / auth as input
            if name.endswith("Request"):
                continue
            leaked = _SECRET_PROPERTY_NAMES & props
            if leaked:
                offenders.append((name, leaked))
        assert offenders == [], offenders

    def test_pushsubscription_read_component_omits_keys(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        read = schema["components"]["schemas"]["PushSubscription"]["properties"]
        assert "p256dh" not in read
        assert "auth" not in read


class TestOpenapiFileHasNoSecrets:
    def test_committed_openapi_yaml_has_no_private_key_material(self):
        text = (ROOT / "openapi.yml").read_text(encoding="utf-8")
        for needle in ("vapid_private_key", "VAPID_PRIVATE_KEY", "PRIVATE KEY"):
            assert needle not in text, needle

    def test_committed_openapi_yaml_response_bodies_omit_subscription_keys(self):
        doc = yaml.safe_load((ROOT / "openapi.yml").read_text(encoding="utf-8"))
        for name, component in doc.get("components", {}).get("schemas", {}).items():
            if name.endswith("Request"):
                continue
            props = set((component or {}).get("properties", {}))
            assert not ({"p256dh", "auth"} & props), name


class TestEnvExampleHygiene:
    def _pairs(self):
        pairs = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            pairs[key.strip()] = value.strip()
        return pairs

    def test_business_contacts_are_placeholders_not_real_values(self):
        pairs = self._pairs()
        phone = pairs.get("BUSINESS_PHONE", "")
        email = pairs.get("BUSINESS_EMAIL", "")
        address = pairs.get("BUSINESS_ADDRESS", "")
        digits = re.sub(r"\D", "", phone)
        # blank, or an unmistakable placeholder (a run of repeated zeros / "x")
        assert phone == "" or "000000" in digits or "x" in phone.lower()
        # no concrete personal mailbox: only blank or an obvious placeholder domain
        assert email == "" or "example" in email.lower()
        # no concrete street address: blank or an obvious placeholder
        assert address == "" or any(
            token in address.lower()
            for token in ("placeholder", "example", "your ", "line 1", "city, state")
        )

    def test_vapid_placeholders_are_blank(self):
        pairs = self._pairs()
        assert pairs.get("VAPID_PUBLIC_KEY", "x") == ""
        assert pairs.get("VAPID_PRIVATE_KEY", "x") == ""
        assert pairs.get("VAPID_ADMIN_EMAIL", "x") == ""

    def test_env_is_gitignored_but_example_is_tracked(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore.splitlines()
        assert "!.env.example" in gitignore.splitlines()


class TestSettingsHaveNoRealContactFallbacks:
    def test_base_settings_default_business_contacts_to_empty(self):
        src = (ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        for key in (
            "BUSINESS_PHONE",
            "BUSINESS_EMAIL",
            "BUSINESS_ADDRESS",
            "BUSINESS_LOGO_PATH",
        ):
            assert re.search(rf'env\(\s*"{key}"\s*,\s*default=""\s*\)', src), key

    def test_vapid_private_key_default_is_empty(self):
        src = (ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        pattern = (
            r'VAPID_PRIVATE_KEY\s*=\s*env\(\s*"VAPID_PRIVATE_KEY"\s*,\s*default=""\s*\)'
        )
        assert re.search(pattern, src)


@pytest.mark.django_db
class TestLogsHaveNoSecrets:
    def test_push_failure_logs_omit_keys_and_private_material(
        self, caplog, monkeypatch
    ):
        from django.test import override_settings

        from apps.notifications.services import webpush
        from apps.notifications.tests.factories import (
            NotificationFactory,
            PushSubscriptionFactory,
        )

        note = NotificationFactory()
        sub = PushSubscriptionFactory(
            user=note.recipient, p256dh="P" * 87, auth="Q" * 22
        )

        def _boom(**kwargs):
            raise webpush.WebPushException("push failed: 500 server error")

        with override_settings(
            PUSH_ENABLED=True,
            VAPID_PRIVATE_KEY="super-secret-private-key-value",
            VAPID_ADMIN_EMAIL="ops@viable-stone.test",
        ):
            monkeypatch.setattr(webpush, "webpush", _boom)
            with caplog.at_level("WARNING"):
                assert webpush.deliver(sub, note) is False

        blob = caplog.text
        assert "super-secret-private-key-value" not in blob
        assert "P" * 87 not in blob
        assert "Q" * 22 not in blob
