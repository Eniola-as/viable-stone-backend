"""Stage 18 — the frozen OpenAPI contract (spec item 8).

``openapi.yml`` at the repo root is the single machine-readable contract. These
tests freeze it: the path/method/operationId surface is snapshotted, so any
intentional change forces a reviewed update to
``openapi_contract_snapshot.json``; regeneration must be byte-stable and emit no
warnings; and no server secret may leak into the published schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO / "openapi.yml"
SNAPSHOT_PATH = Path(__file__).with_name("openapi_contract_snapshot.json")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _live_contract(spec: dict) -> dict:
    paths: dict[str, list[str]] = {}
    op_ids: list[str] = []
    for path, item in spec["paths"].items():
        verbs = sorted(m for m in item if m in _HTTP_METHODS)
        paths[path] = verbs
        for verb in verbs:
            op_ids.append(item[verb]["operationId"])
    return {"paths": paths, "operationIds": sorted(op_ids)}


class TestContractSnapshot:
    def test_path_method_operation_surface_matches_the_snapshot(self, spec, snapshot):
        live = _live_contract(spec)
        added_paths = sorted(set(live["paths"]) - set(snapshot["paths"]))
        removed_paths = sorted(set(snapshot["paths"]) - set(live["paths"]))
        changed = sorted(
            p
            for p in live["paths"]
            if p in snapshot["paths"] and live["paths"][p] != snapshot["paths"][p]
        )
        assert not (added_paths or removed_paths or changed), (
            "The /api/v1/ contract surface changed. If this is intentional, "
            "regenerate openapi.yml and update "
            "tests/acceptance/openapi_contract_snapshot.json in the same commit.\n"
            f"  added paths:   {added_paths}\n"
            f"  removed paths: {removed_paths}\n"
            f"  changed verbs: {changed}"
        )

    def test_operation_ids_match_the_snapshot(self, spec, snapshot):
        live = set(_live_contract(spec)["operationIds"])
        frozen = set(snapshot["operationIds"])
        added = sorted(live - frozen)
        removed = sorted(frozen - live)
        assert not (added or removed), (
            "operationId set changed — review and update the snapshot.\n"
            f"  added:   {added}\n"
            f"  removed: {removed}"
        )


class TestContractIntegrity:
    def test_operation_ids_are_unique(self, spec):
        seen: dict[str, str] = {}
        dupes = []
        for path, item in spec["paths"].items():
            for verb in (m for m in item if m in _HTTP_METHODS):
                oid = item[verb]["operationId"]
                if oid in seen:
                    dupes.append(f"{oid}: {seen[oid]} & {verb.upper()} {path}")
                seen[oid] = f"{verb.upper()} {path}"
        assert not dupes, dupes

    def test_every_path_is_under_api_v1(self, spec):
        stray = [p for p in spec["paths"] if not p.startswith("/api/v1/")]
        assert not stray, stray

    def test_only_cookie_auth_is_advertised(self, spec):
        schemes = spec["components"]["securitySchemes"]
        assert set(schemes) == {"cookieAuth"}
        assert schemes["cookieAuth"]["type"] == "apiKey"

    def test_list_endpoints_use_the_paginated_wrapper(self, spec):
        # every *_list operation returns a Paginated<X>List schema
        offenders = []
        for item in spec["paths"].values():
            get = item.get("get")
            if not get or not get["operationId"].endswith("_list"):
                continue
            ref = json.dumps(get["responses"].get("200", {}))
            if "Paginated" not in ref:
                offenders.append(get["operationId"])
        assert not offenders, offenders

    def test_money_fields_are_decimal_strings(self, spec):
        schemas = spec["components"]["schemas"]
        checks = {
            "SaleRead": ("subtotal", "total", "discount_total"),
            "ProfitReport": ("revenue", "cogs", "gross_profit", "net_profit"),
            "Receipt": ("subtotal", "total"),
        }
        for schema_name, fields in checks.items():
            props = schemas[schema_name]["properties"]
            for field in fields:
                assert props[field]["type"] == "string", (schema_name, field)

    def test_enums_are_documented(self, spec):
        blob = json.dumps(spec["components"]["schemas"])
        assert '"enum"' in blob
        assert "SaleReadStatusEnum" in spec["components"]["schemas"]

    def test_no_server_secret_leaks_into_the_published_schema(self, spec):
        blob = json.dumps(spec).lower()
        for token in (
            "secret_key",
            "database_url",
            "redis_url",
            "offline_signing_key",
            "sentry_dsn",
            "signing_key",
            "code_hash",
            "bin_key",
            "pbkdf2_sha256",
            "argon2",
            "vapid_private",
            "aws_secret_access_key",
        ):
            assert token not in blob, f"schema leaks {token!r}"


class TestContractIsFrozen:
    def test_regeneration_is_byte_stable(self, tmp_path):
        out = tmp_path / "regenerated.yml"
        proc = subprocess.run(
            [
                sys.executable,
                "manage.py",
                "spectacular",
                "--file",
                str(out),
                "--validate",
                "--fail-on-warn",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            "spectacular exited non-zero (validation warning/error):\n"
            + proc.stdout
            + proc.stderr
        )
        regenerated = out.read_text(encoding="utf-8")
        committed = SPEC_PATH.read_text(encoding="utf-8")
        assert regenerated.strip() == committed.strip(), (
            "openapi.yml is stale — run "
            "`python manage.py spectacular --file openapi.yml --validate "
            "--fail-on-warn` and commit the result."
        )
