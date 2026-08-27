"""Stage 17 — local PostgreSQL backup/restore drill.

Proves the documented ``pg_dump`` / ``pg_restore`` procedure round-trips the
authoritative business record: dump the live test database, restore it into a
*separate* scratch database, and assert that key record counts and financial
totals are identical. Skipped when the PostgreSQL client tools are absent.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from decimal import Decimal

import psycopg
import pytest
from django.db import connection

pytestmark = [pytest.mark.backup, pytest.mark.django_db(transaction=True)]

_PG_BIN_HINT = r"C:\Program Files\PostgreSQL\18\bin"


def _tool(name: str) -> str | None:
    return shutil.which(name) or shutil.which(name, path=_PG_BIN_HINT)


_PG_DUMP = _tool("pg_dump")
_PG_RESTORE = _tool("pg_restore")
_HAS_TOOLS = bool(_PG_DUMP and _PG_RESTORE)

skip_no_tools = pytest.mark.skipif(
    not _HAS_TOOLS, reason="pg_dump / pg_restore not available"
)


def _conninfo(dbname: str) -> dict:
    db = connection.settings_dict
    return {
        "host": db["HOST"] or "localhost",
        "port": str(db["PORT"] or "5432"),
        "user": db["USER"],
        "password": db["PASSWORD"],
        "dbname": dbname,
    }


def _env_for(conn: dict) -> dict:
    import os

    return {**os.environ, "PGPASSWORD": conn["password"]}


@skip_no_tools
def test_backup_restores_counts_and_financial_totals(tmp_path):
    # -- arrange known, committed data -------------------------------------- #
    from apps.accounts.tests.factories import (
        BranchFactory,
        EmployeeFactory,
        OwnerFactory,
    )
    from apps.sales.services.sales import CartLine, PaymentLine, create_sale
    from apps.sales.tests.factories import stocked_variant

    branch = BranchFactory(code="BKP")
    owner = OwnerFactory(branch=branch)
    cashier = EmployeeFactory(branch=branch)
    variant = stocked_variant(branch, owner, price="2500.00", quantity=20)
    create_sale(
        branch=branch,
        cashier=cashier,
        cart=[CartLine(variant_id=variant.id, quantity=3)],
        payments=[PaymentLine(method="CASH", amount=Decimal("7500.00"))],
        client_sale_id=uuid.uuid4(),
    )

    live = _conninfo(connection.settings_dict["NAME"])
    scratch_name = f"vs_restore_drill_{uuid.uuid4().hex[:8]}"
    dump_path = tmp_path / "backup.dump"

    def _baseline():
        with psycopg.connect(**live) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM sales_sale")
            sales = cur.fetchone()[0]
            cur.execute("SELECT coalesce(sum(total), 0) FROM sales_sale")
            revenue = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sales_payment")
            payments = cur.fetchone()[0]
            cur.execute("SELECT coalesce(sum(amount), 0) FROM sales_payment")
            paid = cur.fetchone()[0]
        return sales, revenue, payments, paid

    baseline = _baseline()
    assert baseline[0] >= 1 and baseline[1] >= Decimal("7500.00")

    admin = {**live, "dbname": "postgres"}
    try:
        # -- backup ------------------------------------------------------- #
        subprocess.run(
            [
                _PG_DUMP,
                "-h",
                live["host"],
                "-p",
                live["port"],
                "-U",
                live["user"],
                "-Fc",
                "-f",
                str(dump_path),
                live["dbname"],
            ],
            check=True,
            env=_env_for(live),
            capture_output=True,
        )
        assert dump_path.exists() and dump_path.stat().st_size > 0

        # -- restore into a NEW database, never over the original -------- #
        with psycopg.connect(**admin, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{scratch_name}"')
        subprocess.run(
            [
                _PG_RESTORE,
                "-h",
                live["host"],
                "-p",
                live["port"],
                "-U",
                live["user"],
                "-d",
                scratch_name,
                "--no-owner",
                "--no-privileges",
                str(dump_path),
            ],
            check=True,
            env=_env_for(live),
            capture_output=True,
        )

        # -- verify ----------------------------------------------------- #
        restored_conn = {**live, "dbname": scratch_name}
        with psycopg.connect(**restored_conn) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM sales_sale")
            r_sales = cur.fetchone()[0]
            cur.execute("SELECT coalesce(sum(total), 0) FROM sales_sale")
            r_revenue = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sales_payment")
            r_payments = cur.fetchone()[0]
            cur.execute("SELECT coalesce(sum(amount), 0) FROM sales_payment")
            r_paid = cur.fetchone()[0]

        assert (r_sales, r_revenue, r_payments, r_paid) == baseline
    finally:
        with psycopg.connect(**admin, autocommit=True) as c, c.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (scratch_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
