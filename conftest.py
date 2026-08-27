"""Project-wide pytest fixtures."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import (
    BranchFactory,
    EmployeeFactory,
    OwnerFactory,
    TechAdminFactory,
)


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def csrf_client():
    """API client that enforces CSRF, for state-changing security tests."""

    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def branch(db):
    return BranchFactory(code="VS01", name="Viable Stone Main")


@pytest.fixture
def owner(db, branch):
    return OwnerFactory(username="owner", branch=branch, password="owner-pass-12345")


@pytest.fixture
def employee(db, branch):
    return EmployeeFactory(
        username="cashier", branch=branch, password="cashier-pass-12345"
    )


@pytest.fixture
def tech_admin(db):
    return TechAdminFactory(username="tech", password="tech-pass-12345")


@pytest.fixture
def login_as(db):
    """Return a helper that authenticates an APIClient as ``user``.

    By default it also marks the session MFA-verified so business endpoints are
    reachable; pass ``mfa_verified=False`` to exercise the MFA gate.
    """

    def _login(user, *, mfa_verified=True, client=None):
        client = client or APIClient()
        client.force_login(user)
        if mfa_verified:
            session = client.session
            session["mfa_verified"] = True
            session.save()
        return client

    return _login
