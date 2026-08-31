"""G1 — the standard error envelope is a published, reusable OpenAPI component.

Every non-2xx response has the shape ``{code, message, field_errors,
request_id}``. This locks the schema component + the per-operation ``default``
response so a generated client has one typed error model, and checks a couple
of the stable ``code`` strings frontend logic branches on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.django_db

SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "openapi.yml").read_text(encoding="utf-8")
)


def test_error_schema_component_exists_with_the_envelope_shape():
    props = SPEC["components"]["schemas"]["Error"]["properties"]
    assert set(props) == {"code", "message", "field_errors", "request_id"}
    assert SPEC["components"]["responses"]["Error"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/Error"}


def test_every_operation_has_a_default_error_response():
    missing = []
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            default = op.get("responses", {}).get("default")
            if default != {"$ref": "#/components/responses/Error"}:
                missing.append(f"{method.upper()} {path}")
    assert not missing, missing


def test_runtime_error_body_matches_the_documented_envelope(client, django_user_model):
    # an unauthenticated call to a protected route -> 401 envelope
    res = client.get("/api/v1/sales/")
    assert res.status_code == 401
    body = json.loads(res.content)
    assert set(body) == {"code", "message", "field_errors", "request_id"}
    assert body["code"] in {"not_authenticated", "authentication_failed"}


def test_validation_error_code_is_stable(login_as, owner):
    # bad approval filter value -> the documented validation envelope
    res = login_as(owner).get("/api/v1/approvals/?status=NONSENSE")
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "validation_error"
    assert "status" in body["field_errors"]
