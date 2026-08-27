"""Stage 15 — push/notification deep links come only from the allowlist."""

from apps.notifications.action_paths import action_path


def test_known_kinds_build_safe_relative_paths():
    assert action_path("approval", approval_id="A1") == "/approvals/A1"
    assert action_path("sale", sale_id="S1") == "/sales/S1"
    assert action_path("inventory_variant", variant_id="V1") == "/inventory?variant=V1"


def test_unknown_kind_yields_empty_string():
    assert action_path("open_redirect", url="https://evil.example.com") == ""


def test_unexpected_params_yield_empty_string():
    assert action_path("approval", href="https://evil.example.com") == ""
