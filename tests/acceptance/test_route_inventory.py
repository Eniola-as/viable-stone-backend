"""Stage 18 — every /api/v1/ route carries a reviewed security classification."""

from __future__ import annotations

import re

import pytest
from django.urls import get_resolver

from tests.acceptance.security_classification import (
    CLASSIFICATION,
    PUBLIC_ALLOWLIST,
)

pytestmark = pytest.mark.django_db

# Route names that are framework plumbing, not endpoints we classify.
_IGNORED_NAMES = {"api-root", None}
_FORMAT_SUFFIX = re.compile(r"\.<drf_format_suffix.*|\\\.\(\?P<format>")


def _api_route_names() -> set[str]:
    resolver = get_resolver()
    names: set[str] = set()

    def walk(res, under_api: bool):
        for pattern in res.url_patterns:
            frag = str(pattern.pattern)
            if hasattr(pattern, "url_patterns"):
                walk(pattern, under_api or frag.startswith("api/v1/"))
                continue
            if not under_api:
                continue
            name = pattern.name
            if name in _IGNORED_NAMES:
                continue
            # skip the `.json` format-suffix duplicates — same view, same name
            names.add(name)

    walk(resolver, under_api=False)
    # namespaced auth routes come back as bare names; re-add the namespace
    fixed = set()
    for n in names:
        fixed.add(f"auth:{n}" if n in _AUTH_NAMES else n)
    return fixed


_AUTH_NAMES = {
    "csrf",
    "login",
    "logout",
    "me",
    "password-change",
    "mfa-setup",
    "mfa-setup-confirm",
    "mfa-verify",
    "mfa-recovery",
    "mfa-recovery-regenerate",
}
_API_NS_NAMES = {"health", "health-live", "health-ready", "schema", "docs", "redoc"}


def _normalise(names: set[str]) -> set[str]:
    out = set()
    for n in names:
        if n in _API_NS_NAMES:
            out.add(f"api:{n}")
        elif n in _AUTH_NAMES:
            out.add(f"auth:{n}")
        else:
            out.add(n)
    return out


def test_every_api_route_is_classified():
    live = _normalise(_api_route_names())
    unclassified = sorted(live - set(CLASSIFICATION))
    assert not unclassified, (
        "New /api/v1/ route(s) without a security classification — add them to "
        f"tests/acceptance/security_classification.py:\n  {unclassified}"
    )


def test_classification_has_no_stale_entries():
    live = _normalise(_api_route_names())
    stale = sorted(set(CLASSIFICATION) - live)
    assert not stale, f"Classification lists routes that no longer exist: {stale}"


def test_public_allowlist_matches_classification():
    from_class = {name for name, tags in CLASSIFICATION.items() if "public" in tags}
    assert from_class == PUBLIC_ALLOWLIST, (
        "The set of routes tagged 'public' must equal PUBLIC_ALLOWLIST exactly.\n"
        f"  tagged-public not in allowlist: {sorted(from_class - PUBLIC_ALLOWLIST)}\n"
        f"  allowlist not tagged-public:   {sorted(PUBLIC_ALLOWLIST - from_class)}"
    )


def test_every_classification_entry_has_a_role_or_public_tag():
    role_tags = {
        "public",
        "authenticated",
        "owner",
        "tech_admin_or_owner",
        "docs_gated",
    }
    missing = sorted(
        name for name, tags in CLASSIFICATION.items() if not (tags & role_tags)
    )
    assert not missing, f"routes with no access-level tag: {missing}"
