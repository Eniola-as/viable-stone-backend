#!/usr/bin/env bash
# Local backup/restore drill.
#
# Dumps SOURCE_DB, restores it into a brand-new SCRATCH_DB (never over the
# source), then prints comparison counts and financial totals so you can
# confirm the authoritative business record survived.
#
#   SOURCE_DB=viable_stone \
#   PGHOST=127.0.0.1 PGPORT=5432 PGUSER=viable_stone_app PGPASSWORD=... \
#   bash scripts/backup_restore_drill.sh
#
# Nothing here touches Railway. See docs/BACKUP_RESTORE.md.
set -euo pipefail

SOURCE_DB="${SOURCE_DB:?set SOURCE_DB}"
SCRATCH_DB="${SCRATCH_DB:-${SOURCE_DB}_restore_drill}"
STAMP="$(date +%Y%m%dT%H%M%S)"
DUMP="${DUMP:-/tmp/${SOURCE_DB}-${STAMP}.dump}"
GPG_RECIPIENT="${GPG_RECIPIENT:-}"   # optional: encrypt the dump to this key

echo "==> pg_dump ${SOURCE_DB} -> ${DUMP}"
pg_dump -Fc -f "${DUMP}" "${SOURCE_DB}"

if [ -n "${GPG_RECIPIENT}" ]; then
  echo "==> encrypting dump for ${GPG_RECIPIENT}"
  gpg --yes --encrypt --recipient "${GPG_RECIPIENT}" "${DUMP}"
  echo "    encrypted: ${DUMP}.gpg   (upload THIS to independent object storage)"
fi

echo "==> recreate scratch database ${SCRATCH_DB}"
dropdb --if-exists "${SCRATCH_DB}"
createdb "${SCRATCH_DB}"

echo "==> pg_restore into ${SCRATCH_DB}"
pg_restore --no-owner --no-privileges -d "${SCRATCH_DB}" "${DUMP}"

echo "==> verification"
read -r -d '' SQL <<'EOSQL' || true
SELECT 'branches'      AS metric, count(*)::text FROM accounts_branch
UNION ALL SELECT 'users',            count(*)::text FROM accounts_user
UNION ALL SELECT 'sales',            count(*)::text FROM sales_sale
UNION ALL SELECT 'sales_revenue',    coalesce(sum(total),0)::text FROM sales_sale
UNION ALL SELECT 'payments',         count(*)::text FROM sales_payment
UNION ALL SELECT 'payments_total',   coalesce(sum(amount),0)::text FROM sales_payment
UNION ALL SELECT 'stock_movements',  count(*)::text FROM inventory_stockmovement
UNION ALL SELECT 'expenses_total',   coalesce(sum(amount),0)::text FROM finance_expense;
EOSQL

echo "--- source (${SOURCE_DB}) ---"
psql -X -A -F' | ' -t -d "${SOURCE_DB}" -c "${SQL}"
echo "--- restored (${SCRATCH_DB}) ---"
psql -X -A -F' | ' -t -d "${SCRATCH_DB}" -c "${SQL}"

echo
echo "==> compare the two blocks above; every row MUST match."
echo "==> when satisfied:  dropdb ${SCRATCH_DB}"
